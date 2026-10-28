"""Own a model worker subprocess and exchange bounded JSON-lines messages with it."""

import os
import base64
import binascii
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from worker_protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
    request as protocol_request,
    validate_response,
)


class WorkerError(RuntimeError):
    status_code = 500


class WorkerUnavailable(WorkerError):
    pass


class WorkerTimeout(WorkerError):
    status_code = 504


class WorkerRequestError(WorkerError):
    pass


class WorkerCancelled(WorkerError):
    """The caller stopped consuming a live stream; its worker was stopped as well."""

    status_code = 499


class WorkerProcess:
    """A single owned child process. Its stdout is protocol-only; stderr is retained in a log."""

    def __init__(self, command, log_path, *, cwd, env=None, timeouts=None, popen_factory=None):
        self.command = [str(part) for part in command]
        self.log_path = Path(log_path)
        self.cwd = str(cwd)
        self.env = env
        self.timeouts = {"health": 45, "load": 600, "prepare_reference": 120,
                         "generate": 300, "unload": 30, "shutdown": 10}
        if timeouts:
            self.timeouts.update(timeouts)
        self._popen_factory = popen_factory or subprocess.Popen
        self._process = None
        self.runtime_pid = None
        self.runtime_parent_pid = None
        self._log_file = None
        # A small response window also provides backpressure for streaming audio.
        self._responses = queue.Queue(maxsize=4)
        self._request_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closing = threading.Event()
        self._reader_thread = None
        self._request_number = 0

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def returncode(self):
        return self._process.poll() if self._process is not None else None

    @property
    def alive(self):
        return self._process is not None and self._process.poll() is None

    def _tail_log(self):
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-16:])
        except OSError:
            return ""

    def start(self):
        with self._lifecycle_lock:
            if self._closing.is_set():
                raise WorkerUnavailable(f"Worker has already been closed. Log: {self.log_path}")
            if self.alive:
                return self
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self.log_path.open("a", encoding="utf-8", errors="replace")
            self._log_file.write(f"\n--- worker launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self._log_file.write(f"command={self.command!r}\n")
            self._log_file.flush()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self._process = self._popen_factory(
                    self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self._log_file,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                )
            except Exception:
                self._log_file.close()
                self._log_file = None
                raise
            self._responses = queue.Queue(maxsize=4)
            self._reader_thread = threading.Thread(target=self._read_responses, name=f"worker-stdout-{self._process.pid}", daemon=True)
            self._reader_thread.start()
        try:
            self.call("health")
        except Exception:
            self.close(force=True)
            raise
        return self

    def _read_responses(self):
        process = self._process

        def enqueue(item):
            while True:
                try:
                    self._responses.put(item, timeout=0.1)
                    return
                except queue.Full:
                    if self._closing.is_set():
                        return

        try:
            for line in process.stdout:
                if len(line.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
                    enqueue(ProtocolError("Worker emitted a protocol line beyond the size limit."))
                    break
                enqueue(line)
        except Exception as exc:
            enqueue(exc)
        finally:
            enqueue(None)

    def call_stream(self, method, on_chunk, **params):
        """Call a worker method that emits ordered PCM chunks before its final response.

        The callback runs on the calling thread. Returning False stops the owned worker,
        which is the only safe cancellation mechanism while a model generator is active.
        """
        if method != "generate_stream":
            raise ValueError("Only generate_stream supports the worker streaming contract.")
        if not callable(on_chunk):
            raise TypeError("on_chunk must be callable.")
        if not self.alive or self._closing.is_set():
            raise WorkerUnavailable(f"Worker is not running. Log: {self.log_path}")
        with self._request_lock:
            self._request_number += 1
            request_id = f"{self.pid}-{self._request_number}-{uuid.uuid4().hex[:8]}"
            try:
                line = encode_message(protocol_request(request_id, method, **params))
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ProtocolError) as exc:
                raise WorkerUnavailable(f"Could not send {method} to worker. Log: {self.log_path}\n{self._tail_log()}") from exc

            deadline = time.monotonic() + self.timeouts.get(method, self.timeouts.get("generate", 300))
            expected_sequence = 0
            expected_sample_start = 0
            sample_rate = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close(force=True)
                    timeout = self.timeouts.get(method, self.timeouts.get("generate", 300))
                    raise WorkerTimeout(f"Worker timed out during {method} after {timeout}s. Log: {self.log_path}\n{self._tail_log()}")
                try:
                    raw = self._responses.get(timeout=remaining)
                except queue.Empty as exc:
                    self.close(force=True)
                    timeout = self.timeouts.get(method, self.timeouts.get("generate", 300))
                    raise WorkerTimeout(f"Worker timed out during {method} after {timeout}s. Log: {self.log_path}\n{self._tail_log()}") from exc
                if raw is None:
                    raise WorkerUnavailable(f"Worker exited during {method} (code {self.returncode}). Log: {self.log_path}\n{self._tail_log()}")
                if isinstance(raw, Exception):
                    raise WorkerUnavailable(f"Worker protocol reader failed: {raw}. Log: {self.log_path}\n{self._tail_log()}") from raw
                try:
                    message = decode_message(raw)
                except ProtocolError as exc:
                    self.close(force=True)
                    raise WorkerUnavailable(f"Invalid worker response during {method}: {exc}. Log: {self.log_path}") from exc
                if message.get("protocol_version") != PROTOCOL_VERSION or str(message.get("id")) != str(request_id):
                    self.close(force=True)
                    raise WorkerUnavailable(f"Invalid worker response identity during {method}. Log: {self.log_path}")
                if message.get("event") == "audio_chunk":
                    try:
                        sequence = message["sequence"]
                        start = message["sample_start"]
                        count = message["sample_count"]
                        rate = message["sample_rate"]
                        if (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != expected_sequence or
                                not isinstance(start, int) or isinstance(start, bool) or start != expected_sample_start or
                                isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= 8192 or
                                isinstance(rate, bool) or not isinstance(rate, int) or not 8000 <= rate <= 192000 or
                                message.get("sample_format") != "pcm_s16le"):
                            raise ValueError("invalid sequence, sample offset, size, rate, or format")
                        if sample_rate is not None and rate != sample_rate:
                            raise ValueError("sample rate changed during a stream")
                        encoded = message["data"]
                        if not isinstance(encoded, str):
                            raise ValueError("audio data is not base64 text")
                        pcm = base64.b64decode(encoded, validate=True)
                        if len(pcm) != count * 2:
                            raise ValueError("PCM byte length does not match sample_count")
                    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                        self.close(force=True)
                        raise WorkerUnavailable(f"Invalid audio chunk during {method}: {exc}. Log: {self.log_path}") from exc
                    packet = {key: value for key, value in message.items()
                              if key not in {"protocol_version", "id", "event", "data"}}
                    packet["data"] = encoded
                    packet["pcm"] = pcm
                    packet["sequence"] = sequence
                    packet["sample_start"] = start
                    packet["sample_count"] = count
                    packet["sample_rate"] = rate
                    try:
                        accepted = on_chunk(packet)
                    except Exception as exc:
                        self.close(force=True)
                        raise WorkerCancelled(f"Audio chunk consumer stopped: {type(exc).__name__}: {exc}") from exc
                    if accepted is False:
                        self.close(force=True)
                        raise WorkerCancelled(f"Stream consumption was cancelled; worker stopped. Log: {self.log_path}")
                    expected_sequence += 1
                    expected_sample_start += count
                    sample_rate = rate
                    continue
                try:
                    message = validate_response(message, request_id)
                except ProtocolError as exc:
                    self.close(force=True)
                    raise WorkerUnavailable(f"Invalid worker response during {method}: {exc}. Log: {self.log_path}") from exc
                if not message["ok"]:
                    error = message.get("error") or {}
                    detail = (f"{error.get('type', 'WorkerError')}: {error.get('message', 'request failed')}"
                              if isinstance(error, dict) else str(error))
                    raise WorkerRequestError(f"Worker {method} failed: {detail}. Log: {self.log_path}")
                result = message.get("result")
                if not isinstance(result, dict):
                    self.close(force=True)
                    raise WorkerUnavailable(f"Worker returned a malformed final stream response. Log: {self.log_path}")
                if result.get("sample_count") != expected_sample_start or result.get("chunk_count") != expected_sequence:
                    self.close(force=True)
                    raise WorkerUnavailable("Worker final stream counts do not match delivered chunks.")
                if sample_rate is not None and result.get("sample_rate") != sample_rate:
                    self.close(force=True)
                    raise WorkerUnavailable("Worker final sample rate does not match delivered chunks.")
                if isinstance(result.get("pid"), int):
                    self.runtime_pid = result["pid"]
                if isinstance(result.get("parent_pid"), int):
                    self.runtime_parent_pid = result["parent_pid"]
                return result

    def call(self, method, **params):
        if not self.alive or self._closing.is_set():
            raise WorkerUnavailable(f"Worker is not running. Log: {self.log_path}")
        with self._request_lock:
            self._request_number += 1
            request_id = f"{self.pid}-{self._request_number}-{uuid.uuid4().hex[:8]}"
            try:
                line = encode_message(protocol_request(request_id, method, **params))
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ProtocolError) as exc:
                raise WorkerUnavailable(f"Could not send {method} to worker. Log: {self.log_path}\n{self._tail_log()}") from exc
            timeout = self.timeouts.get(method, 30)
            try:
                raw = self._responses.get(timeout=timeout)
            except queue.Empty as exc:
                self.close(force=True)
                raise WorkerTimeout(f"Worker timed out during {method} after {timeout}s. Log: {self.log_path}\n{self._tail_log()}") from exc
            if raw is None:
                raise WorkerUnavailable(f"Worker exited during {method} (code {self.returncode}). Log: {self.log_path}\n{self._tail_log()}")
            if isinstance(raw, Exception):
                raise WorkerUnavailable(f"Worker protocol reader failed: {raw}. Log: {self.log_path}\n{self._tail_log()}") from raw
            try:
                message = validate_response(decode_message(raw), request_id)
            except ProtocolError as exc:
                raise WorkerUnavailable(f"Invalid worker response during {method}: {exc}. Log: {self.log_path}\n{self._tail_log()}") from exc
            if not message["ok"]:
                error = message.get("error") or {}
                if isinstance(error, dict):
                    detail = f"{error.get('type', 'WorkerError')}: {error.get('message', 'request failed')}"
                else:
                    detail = str(error)
                raise WorkerRequestError(f"Worker {method} failed: {detail}. Log: {self.log_path}")
            result = message.get("result")
            if isinstance(result, dict):
                if isinstance(result.get("pid"), int):
                    self.runtime_pid = result["pid"]
                if isinstance(result.get("parent_pid"), int):
                    self.runtime_parent_pid = result["parent_pid"]
            return result

    def _terminate_runtime_child(self):
        """Terminate the reported Python child only when the worker confirms its launcher parent."""
        process = self._process
        if (os.name != "nt" or process is None or not self.runtime_pid or
                self.runtime_pid == process.pid or self.runtime_parent_pid != process.pid):
            return
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, self.runtime_pid)  # TERMINATE | SYNCHRONIZE
            if not handle:
                return
            try:
                if kernel32.WaitForSingleObject(handle, 0) == 0x00000102:
                    kernel32.TerminateProcess(handle, 1)
                    kernel32.WaitForSingleObject(handle, 5000)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # The owned launcher is still killed below; this fallback never searches for or kills
            # arbitrary Python processes.
            pass

    def close(self, *, force=False):
        if not force and self.alive:
            try:
                self.call("shutdown")
            except Exception:
                return self.close(force=True)
        with self._lifecycle_lock:
            process = self._process
            self._closing.set()
        if process is None:
            return
        if process.poll() is None:
            if force:
                self._terminate_runtime_child()
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5 if force else 12)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        self._closing.set()
        for pipe_name in ("stdin", "stdout"):
            pipe = getattr(process, pipe_name, None)
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if self._log_file is not None:
            try:
                self._log_file.flush()
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
