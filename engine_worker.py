"""Child process entry point. stdout carries JSON-lines protocol messages only."""

import argparse
import base64
import importlib.util
import os
import sys
import traceback
from pathlib import Path

MAX_STREAM_SAMPLES = 8192


def main():
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if args.engine == "voxcpm2":
        # VoxCPM2 runs from its pinned source with the packages of its own .venv-voxcpm. A .venv-voxcpm made by hand without
        # PyTorch borrows .venv-fqwen's packages instead, read-only (the setup it was first checked with, on an AMD card); only
        # this worker sees them.
        if importlib.util.find_spec("torch") is None:
            sys.path.append(str(root / ".venv-fqwen" / "Lib" / "site-packages"))
        sys.path.insert(0, str(root / "out" / "voxcpm2" / "source" / "src"))
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "MODELSCOPE_OFFLINE"):
            os.environ[name] = "1"
    from worker_protocol import PROTOCOL_VERSION, decode_message, encode_message, response
    from worker_runtime import EngineWorkerRuntime

    runtime = EngineWorkerRuntime(args.engine, args.device)
    for raw in sys.stdin:
        request_id = None
        try:
            request = decode_message(raw.rstrip("\r\n"))
            if request.get("protocol_version") != PROTOCOL_VERSION:
                raise ValueError(f"Unsupported protocol version {request.get('protocol_version')!r}.")
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Worker request params must be an object.")
            if method == "generate_stream":
                import numpy as np
                sequence = 0
                sample_start = 0

                def emit_audio(audio, sample_rate, chunk_info=None):
                    nonlocal sequence, sample_start
                    values = np.asarray(audio, dtype=np.float32).reshape(-1)
                    for offset in range(0, values.size, MAX_STREAM_SAMPLES):
                        segment = values[offset:offset + MAX_STREAM_SAMPLES]
                        if segment.size == 0 or not np.isfinite(segment).all():
                            raise ValueError("Worker stream received empty or non-finite audio samples.")
                        pcm = np.rint(np.clip(segment, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()
                        event = {
                            "protocol_version": PROTOCOL_VERSION,
                            "id": str(request_id),
                            "event": "audio_chunk",
                            "sequence": sequence,
                            "sample_start": sample_start,
                            "sample_count": len(pcm) // 2,
                            "sample_rate": int(sample_rate),
                            "sample_format": "pcm_s16le",
                            "data": base64.b64encode(pcm).decode("ascii"),
                        }
                        if sequence == 0 and isinstance(chunk_info, dict):
                            event.update({key: chunk_info[key] for key in
                                          ("generator_first_chunk_s", "backend_chunk_index")
                                          if key in chunk_info})
                        protocol_stdout.write(encode_message(event))
                        protocol_stdout.flush()
                        sample_start += len(pcm) // 2
                        sequence += 1

                result, should_stop = runtime.dispatch(method, params, emit_audio=emit_audio)
                if result.get("sample_count") != sample_start:
                    raise RuntimeError("Worker stream sample count changed during transport.")
                result["sample_count"] = sample_start
                result["chunk_count"] = sequence
                result["sample_rate"] = runtime.sample_rate
            else:
                result, should_stop = runtime.dispatch(method, params)
            protocol_stdout.write(encode_message(response(request_id, result=result)))
            protocol_stdout.flush()
            if should_stop:
                break
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            request_id = locals().get("request_id")
            try:
                payload = response(request_id, error={"type": type(exc).__name__, "message": str(exc)[:2000]})
                protocol_stdout.write(encode_message(payload))
                protocol_stdout.flush()
            except Exception:
                break


if __name__ == "__main__":
    main()
