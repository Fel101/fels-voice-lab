"""Broker-side scheduler, worker lifecycle, and audio handoff for the local HTTP service."""

import json
import os
import threading
import time
import uuid
from pathlib import Path

from worker_process import (WorkerCancelled, WorkerError, WorkerProcess, WorkerRequestError,
                            WorkerTimeout, WorkerUnavailable)
from chatterbox_support import CHATTERBOX_SAMPLING_FIELDS, normalize_chatterbox_sampling
from phonon_support import PHONON_ENGINE_ID, check_request as check_phonon_request
from reference_paths import resolve_reference_path, REFERENCE_ROOTS_ENV, CONVERSION_SOURCE_ROOTS_ENV
from local_ownership import LocalOwnership
import engine_checks

LAB = Path(__file__).resolve().parent
OUT = LAB / "out"
CATALOG_PATH = LAB / "catalog" / "engines.json"
QWEN_MODES = {
    "qwen3-tts-1.7b-voice-design": "design",
    "qwen3-tts-1.7b-base": "clone",
    "qwen3-tts-0.6b-base": "clone",
    "qwen3-tts-1.7b-custom-voice": "custom_voice",
}
VOXCPM_MODES = {"design", "clone", "continuation"}


def process_is_running(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT = still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def drop_scratch(key, pid):
    """Remove a stopped worker's temporary-audio folder. The audio handoff deletes its own files, so the folder is
    empty; rmdir refuses anything else, and whatever a crash left behind stays for the sweep in housekeeping.py."""
    if pid:
        try:
            (OUT / "workers" / f"{key}-{pid}").rmdir()
        except OSError:
            pass


class WorkerBusy(WorkerError):
    status_code = 409


def read_catalog():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("catalog_version") != 1 or catalog.get("worker_protocol_version") != 1:
        raise RuntimeError("Unsupported engine catalog or worker protocol version.")
    engines = catalog.get("engines")
    if not isinstance(engines, list) or not engines:
        raise RuntimeError("The engine catalog is empty or malformed.")
    ids = [engine.get("id") for engine in engines]
    if len(ids) != len(set(ids)) or any(not isinstance(key, str) or not key for key in ids):
        raise RuntimeError("The engine catalog contains missing or duplicate engine IDs.")
    return catalog


class EnginePool:
    """Keep compatible model workers separate and allow one load/generation job at a time."""

    def __init__(self, device, idle_minutes, *, worker_factory=None, resource_check=None,
                 catalog=None, worker_timeouts=None, idle_poll_seconds=30,
                 residency_lock_path=None, reference_roots=(), conversion_source_roots=(), media_guard=None,
                 check_store=None, install_probe=None, fingerprint_probe=None, ram_needs=None, ram_probe=None):
        self.device = device
        self.catalog = catalog or read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: value for key, value in self.entries.items()
                            if value.get("integration", {}).get("service_api") is True}
        self.workers = {}
        self.last_used = time.time()
        self.idle = idle_minutes * 60
        self.worker_factory = worker_factory
        self.resource_check = resource_check
        self.worker_timeouts = worker_timeouts or {}
        self.idle_poll_seconds = idle_poll_seconds
        self.reference_roots = tuple(Path(root).expanduser().resolve() for root in reference_roots)
        self.conversion_source_roots = tuple(Path(root).expanduser().resolve() for root in conversion_source_roots)
        self.media_guard = media_guard  # Called with each resolved reference path; Cast uses it to refuse retired recordings.
        self.check_store = check_store  # Optional EngineCheckStore for check results and clip timings.
        self.install_probe = install_probe or (lambda entry: engine_checks.installation(entry, LAB))
        # Names the model pin and installed packages a check was made against; a stored check is out of date once they change.
        self.fingerprint_probe = fingerprint_probe or (lambda entry: engine_checks.engine_fingerprint(entry, LAB))
        # The free RAM in GB the Lab wants before it loads each engine, and a call that reports this computer's RAM. The Models
        # page shows them on the cards; neither changes what a load does (resource_check does that).
        self.ram_needs = dict(ram_needs or {})
        self.ram_probe = ram_probe
        self._gpu_job = threading.Lock()
        self._held_for = None
        # Model residency, not just individual calls, is exclusive across local services.
        lock_path = residency_lock_path or (OUT / "runtime-locks" / "model-residency.lock")
        self._residency = LocalOwnership(lock_path) if worker_factory is None or residency_lock_path else None
        self._state_lock = threading.RLock()
        self._starting_workers = set()
        self._closed = threading.Event()
        self._last_error = None
        self._last_unload = []
        self._reaper = threading.Thread(target=self._reap_idle, name="voice-worker-reaper", daemon=True)
        self._reaper.start()

    @property
    def busy(self):
        return self._gpu_job.locked()

    def _resolve_reference_path(self, key, path):
        resolved = resolve_reference_path(key, path, extra_roots=self.reference_roots)
        if self.media_guard is not None:
            self.media_guard(resolved)
        return resolved

    @property
    def loaded(self):
        with self._state_lock:
            return sorted(key for key, record in self.workers.items() if record["loaded"])

    def loaded_model_info(self, key):
        """Return the last checkpoint-discovered metadata without loading a new worker."""
        with self._state_lock:
            record = self.workers.get(key)
            if not record or not record.get("loaded"):
                return None
            return dict(record.get("load_result") or {})

    def _new_worker(self, key, entry):
        if self.worker_factory is not None:
            return self.worker_factory(entry)
        python_path = LAB / entry["runtime"]["python"]
        if not python_path.is_file():
            raise WorkerUnavailable(f"Configured Python environment is missing for {key}: {python_path}")
        command = [str(python_path), "-u", str(LAB / "engine_worker.py"), "--engine", key, "--device", self.device]
        env = os.environ.copy()
        # Replace any inherited allowlist; only this service's configured roots apply.
        env[REFERENCE_ROOTS_ENV] = json.dumps([str(root) for root in self.reference_roots])
        env[CONVERSION_SOURCE_ROOTS_ENV] = json.dumps([str(root) for root in self.conversion_source_roots])
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env.setdefault("HF_HOME", str(LAB / "hf-cache"))
        env.setdefault("PYTHONIOENCODING", "utf-8")
        log_dir = OUT / "logs" / "workers" / key
        log_path = log_dir / f"worker-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log"
        return WorkerProcess(command, log_path, cwd=LAB, env=env, timeouts=self.worker_timeouts)

    def _record_clip(self, key, info, samples, sample_rate):
        """Add one finished clip's own generation time to the speed table."""
        if self.check_store is None or not isinstance(info, dict):
            return
        total = info.get("total_s")
        if isinstance(total, bool) or not isinstance(total, (int, float)) or not samples or not sample_rate:
            return
        self.check_store.record_clip(key, engine_checks.clip_device(info, self.device), total, samples / sample_rate)

    def _require_api_engine(self, key):
        if not isinstance(key, str) or key not in self.api_entries:
            if isinstance(key, str) and key in self.entries:
                raise ValueError(f"Engine {key!r} is catalog-only; it is not enabled by this service.")
            raise ValueError(f"Unknown engine {key!r}.")

    def _raise_with_install_pointer(self, entry, exc):
        """Re-raise a failure to bring an engine up. When the engine has a launcher and its files are missing, the same
        kind of error leads with a sentence saying where to install it, so the first thing someone who has not installed it
        sees says what to do."""
        pointer = None
        if isinstance(exc, WorkerError):
            try:
                pointer = engine_checks.install_pointer(entry, self.install_probe)
            except Exception:
                pass  # a probe that fails must not hide the error it would have explained
        if pointer:
            raise type(exc)(f"{pointer} {exc}") from exc
        raise exc

    def _ensure_loaded_locked(self, key, load_params=None):
        if self._closed.is_set():
            raise WorkerUnavailable("Voice service is shutting down.")
        self._require_api_engine(key)
        if self._residency and not self._residency.acquire():
            raise WorkerBusy("Another voice-lab service owns the model worker. Unload its model or wait for idle unload.")
        entry = self.api_entries[key]
        load_params = dict(load_params or {})
        record = self.workers.get(key)
        if record and record["loaded"] and record["worker"].alive:
            requested_profile = load_params.get("profile")
            if requested_profile is None or record.get("profile") == requested_profile:
                return record["worker"]
        if record:
            self._discard_locked(key, record["worker"], release_ownership=False)
        if self.workers:
            self._unload_locked(release_ownership=False)
        try:
            if self.resource_check:
                self.resource_check(key)
            worker = self._new_worker(key, entry)
        except Exception as exc:
            if self._residency:
                self._residency.release()
            self._raise_with_install_pointer(entry, exc)
        with self._state_lock:
            if self._closed.is_set():
                worker.close(force=True)
                if self._residency:
                    self._residency.release()
                raise WorkerUnavailable("Voice service is shutting down.")
            self._starting_workers.add(worker)
        try:
            worker.start()
            result = worker.call("load", **load_params)
        except Exception as exc:
            with self._state_lock:
                self._starting_workers.discard(worker)
            worker.close(force=True)
            failed_pid = getattr(worker, "runtime_pid", None) or worker.pid
            if worker.alive or process_is_running(failed_pid):
                with self._state_lock:
                    self.workers[key] = {"worker": worker, "loaded": False,
                        "runtime_pid": failed_pid, "profile": None, "load_result": {}, "last_result": {}}
            elif self._residency:
                self._residency.release()
            self._last_error = {"engine": key, "message": "worker failed to start/load", "log": str(getattr(worker, "log_path", ""))}
            self._raise_with_install_pointer(entry, exc)
        with self._state_lock:
            self._starting_workers.discard(worker)
            shutting_down = self._closed.is_set()
            if not shutting_down:
                self.workers[key] = {"worker": worker, "loaded": True,
                                     "runtime_pid": result.get("pid", worker.pid),
                                     "profile": result.get("profile", load_params.get("profile")),
                                     "load_result": result, "last_result": result}
        if shutting_down:
            worker.close(force=True)
            raise WorkerUnavailable("Voice service shut down while the worker was loading.")
        self._last_error = None
        return worker

    def _discard_locked(self, key, worker, release_ownership=True):
        with self._state_lock:
            current = self.workers.get(key)
        worker.close(force=True)
        runtime_pid = (current or {}).get("runtime_pid") or getattr(worker, "runtime_pid", None) or worker.pid
        if worker.alive or process_is_running(runtime_pid):
            raise WorkerUnavailable(f"Worker {key} did not stop; model ownership is retained until its process exits.")
        drop_scratch(key, runtime_pid)
        with self._state_lock:
            if current and self.workers.get(key) is current and current["worker"] is worker:
                del self.workers[key]
        if release_ownership and not self.workers and self._residency:
            self._residency.release()

    def _acquire_job(self):
        if not self._gpu_job.acquire(blocking=False):
            held = self._held_for
            raise WorkerBusy(f"The GPU worker is busy while {held}." if held
                             else "The GPU worker is busy with another load or generation request.")

    def hold(self, reason):
        """Keep every load, test and clip out while something long changes what is installed, such as an install from the
        Models page. `reason` finishes the sentence "The GPU worker is busy while ..." in the refusals. Raises WorkerBusy when a
        request already holds the worker; release_hold() gives it back."""
        self._acquire_job()
        self._held_for = reason

    def release_hold(self):
        self._held_for = None
        self._gpu_job.release()

    def load(self, key, profile=None):
        self._acquire_job()
        try:
            self.last_used = time.time()
            load_params = {"profile": profile or "eager"} if key == "breeze-tts-2" else None
            worker = self._ensure_loaded_locked(key, load_params)
            record = self.workers[key]
            result = record.get("load_result") or {}
            return {"engine": key, "loaded": True, "worker_pid": record.get("runtime_pid"),
                    "launcher_pid": worker.pid,
                    "sample_rate": result.get("sample_rate"), "load_seconds": result.get("load_seconds"),
                    "memory": result.get("memory", {}), "profile": record.get("profile"),
                    "backend": result.get("backend"),
                    "checkpoint_revision": result.get("checkpoint_revision"),
                    "checkpoint": result.get("checkpoint"),
                    "runtime_identity": result.get("runtime_identity"),
                    "supported_speakers": result.get("supported_speakers", []),
                    "supported_languages": result.get("supported_languages", [])}
        finally:
            self._gpu_job.release()

    def check(self, key):
        """Ask whether one engine can speak now on this device: its files first, then a real load and one short clip."""
        self._require_api_engine(key)
        entry = self.api_entries[key]
        if entry.get("integration", {}).get("task_only") is True:
            raise ValueError("Voice conversion is a source-audio task with no speech check; use Voice conversion.")
        installed = self.install_probe(entry)
        if not installed["installed"]:
            return {"status": "not_installed", "device": self.device, "missing": installed["missing"],
                    "checked_at": engine_checks.utc_now()}
        fingerprint = self.fingerprint_probe(entry)  # taken before the load, so it names what the check ran against
        try:
            loaded = self.load(key)
            wav, sample_rate, info = self.speak(engine_checks.smoke_request(key, loaded, root=LAB))
        except WorkerBusy:
            raise
        except Exception as exc:
            record = engine_checks.check_record(status="failed", device=self.device,
                                                reason=engine_checks.failure_reason(exc), fingerprint=fingerprint)
        else:
            problem = engine_checks.audible(wav, sample_rate)
            record = engine_checks.check_record(
                status="failed" if problem else "passed", device=self.device,
                ran_on=engine_checks.clip_device(info, self.device), reason=problem,
                load_seconds=loaded.get("load_seconds"), total_s=info.get("total_s"),
                audio_seconds=wav.size / sample_rate, fingerprint=fingerprint)
        if self.check_store is not None:
            self.check_store.record_check(key, self.device, record)
        return record

    def engine_status(self):
        """Installed, usable and measured state for every catalog engine. Reads files and stored results only."""
        return engine_checks.status_payload(self.entries, device=self.device, busy=self.busy,
                                            store=self.check_store, probe=self.install_probe,
                                            fingerprint_of=self.fingerprint_probe, ram_needs=self.ram_needs,
                                            ram=self.ram_probe() if self.ram_probe else None)

    def prepare_reference(self, key, path, *, exaggeration=0.5, profile=None,
                          reference_text="", xvec_only=False):
        self._acquire_job()
        try:
            self.last_used = time.time()
            self._require_api_engine(key)
            if self.api_entries[key].get("capabilities", {}).get("voice_cloning", {}).get("status") == "unsupported":
                raise ValueError(f"Engine {key!r} does not support reference audio.")
            reference = self._resolve_reference_path(key, path)
            if key=='drama-3' and (not isinstance(reference_text,str) or not reference_text.strip()):
                raise ValueError('Drama 3 reference preparation requires the exact transcript.')
            load_params = {"profile": profile or "eager"} if key == "breeze-tts-2" else None
            worker = self._ensure_loaded_locked(key, load_params)
            if key == "breeze-tts-2":
                return worker.call("prepare_reference", path=str(reference))
            if key == 'drama-3':
                return worker.call('prepare_reference', path=str(reference), reference_text=reference_text)
            if key in QWEN_MODES:
                return worker.call("prepare_reference", path=str(reference),
                                   reference_text=reference_text, xvec_only=xvec_only)
            if key == "voxcpm2":
                return worker.call("prepare_reference", path=str(reference))
            return worker.call("prepare_reference", path=str(reference), exaggeration=exaggeration)
        except (WorkerUnavailable, WorkerTimeout):
            record = self.workers.get(key)
            if record:
                self._discard_locked(key, record["worker"])
            raise
        finally:
            self._gpu_job.release()

    def speak(self, req):
        if not isinstance(req, dict):
            raise ValueError("Request body must be a JSON object.")
        key = req.get("engine", "kokoro")
        self._require_api_engine(key)
        if key == "chatterbox-vc":
            raise ValueError("Chatterbox Voice Conversion is a source-audio task; use POST /api/convert.")
        text = req.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Type something to say.")
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters (split long replies into sentences).")
        if key == 'drama-3':
            from drama3_runtime import controls
            allowed = {'engine','text','mode','voice_description','direction','reference_path','reference_text','language',*controls()}
            if set(req)-allowed:
                raise ValueError('Unsupported Drama 3 parameter(s): ' + ', '.join(sorted(set(req)-allowed)))
            mode = req.get('mode','design')
            if mode not in ('design','clone'):
                raise ValueError('Drama 3 mode must be design or clone.')
            if mode=='clone':
                self._resolve_reference_path(key,req.get('reference_path'))
                if not isinstance(req.get('reference_text'),str) or not req['reference_text'].strip():
                    raise ValueError('Drama 3 cloning requires the exact reference transcript.')
            controls({name:req[name] for name in controls() if name in req})
        if key in QWEN_MODES:
            mode = QWEN_MODES[key]
            if req.get("mode", mode) != mode:
                raise ValueError(f"Checkpoint {key!r} supports mode {mode!r}; it does not support {req.get('mode')!r}.")
            common_fields = {"engine", "text", "mode", "language", "seed", "max_new_tokens", "save_voice_as",
                             "temperature", "top_k", "top_p", "repetition_penalty"}
            mode_fields = {
                "design": {"voice_description"},
                "clone": {"reference_path", "reference_text", "xvec_only", "direction"},
                "custom_voice": {"speaker", "direction"},
            }[mode]
            unsupported = set(req) - common_fields - mode_fields
            if unsupported:
                raise ValueError(f"Unsupported Qwen {mode} parameter(s): {', '.join(sorted(unsupported))}.")
            if mode == "design" and not str(req.get("voice_description", "")).strip():
                raise ValueError("Qwen VoiceDesign requires a non-empty voice_description.")
            if mode == "clone":
                if not req.get("reference_path"):
                    raise ValueError("Qwen reference cloning requires reference_path.")
                if not isinstance(req.get("xvec_only", False), bool):
                    raise ValueError("xvec_only must be true or false.")
                if not req.get("xvec_only", False) and not str(req.get("reference_text", "")).strip():
                    raise ValueError("Qwen reference cloning requires reference_text unless xvec_only is enabled.")
            if mode == "custom_voice" and not str(req.get("speaker", "")).strip():
                raise ValueError("Qwen CustomVoice requires a speaker returned by the loaded checkpoint.")
            if req.get("save_voice_as") and mode != "design":
                raise ValueError("Only a Qwen VoiceDesign take can be saved as a new reusable voice reference.")
        elif key == "voxcpm2":
            mode = req.get("mode", "design")
            if mode not in VOXCPM_MODES:
                raise ValueError("VoxCPM2 mode must be design, clone, or continuation.")
            common_fields = {"engine", "text", "mode", "seed", "cfg_value", "inference_timesteps",
                             "save_voice_as"}
            mode_fields = {
                "design": {"voice_description"},
                "clone": {"reference_path", "direction"},
                "continuation": {"reference_path", "prompt_path", "prompt_text"},
            }[mode]
            unsupported = set(req) - common_fields - mode_fields
            if unsupported:
                raise ValueError(f"Unsupported VoxCPM2 {mode} parameter(s): {', '.join(sorted(unsupported))}.")
            if mode == "design" and not str(req.get("voice_description", "")).strip():
                raise ValueError("VoxCPM2 design requires a non-empty voice_description.")
            if mode in {"clone", "continuation"} and not req.get("reference_path"):
                raise ValueError(f"VoxCPM2 {mode} requires reference_path.")
            if mode == "continuation":
                if not req.get("prompt_path") or not str(req.get("prompt_text", "")).strip():
                    raise ValueError("VoxCPM2 continuation requires prompt_path and prompt_text.")
            direction = req.get("direction", "")
            if mode == "clone" and (not isinstance(direction, str) or len(direction) > 1000):
                raise ValueError("VoxCPM2 direction must be text of 1000 characters or fewer.")
            if req.get("save_voice_as") and mode != "design":
                raise ValueError("Only a VoxCPM2 design take can be saved as a reusable voice reference.")
        elif key in {"orig", "turbo"}:
            all_sampler_fields = set().union(*CHATTERBOX_SAMPLING_FIELDS.values())
            chatterbox_sampling = normalize_chatterbox_sampling(
                key, {name: req[name] for name in all_sampler_fields if name in req})
            if key == "turbo" and req.get("clone", True):
                reference = req.get("reference_path") or LAB / "reference" / "af_heart.wav"
                if req.get("reference_path") or Path(reference).is_file():
                    self._resolve_reference_path(key, reference)
        elif key == PHONON_ENGINE_ID:
            phonon_params = check_phonon_request(req)
        self._acquire_job()
        output_file = None
        try:
            self.last_used = time.time()
            profile = str(req.get("profile", "eager") or "eager") if key == "breeze-tts-2" else None
            load_params = {"profile": profile} if profile is not None else None
            worker = self._ensure_loaded_locked(key, load_params)
            params = {"text": text.strip()}
            if key == "kokoro":
                params["voice"] = str(req.get("voice", "af_heart"))
                params["speed"] = req.get("speed", 1.0)
                params["language"] = req.get("language", "English")
            elif key == PHONON_ENGINE_ID:
                params.update(phonon_params)
            elif key == 'drama-3':
                params.update({name:value for name,value in req.items() if name not in ('engine','text','language')})
                if params.get('mode')=='clone':
                    params['reference_path']=str(self._resolve_reference_path(key,params.get('reference_path')))
            elif key == "breeze-tts-2":
                mode = str(req.get("mode", "design"))
                params.update({
                    "mode": mode,
                    "profile": profile,
                    "voice_description": req.get("voice_description", ""),
                    "direction": req.get("direction", ""),
                    "cfg_scale": req.get("cfg_scale", 1.0 if mode == "clone" else 4.0),
                    "seed": req.get("seed", 42),
                    "max_new_tokens": req.get("max_new_tokens", 1500),
                    "temperature": req.get("temperature", 0.9),
                    "top_k": req.get("top_k", 50),
                    "top_p": req.get("top_p", 1.0),
                    "repetition_penalty": req.get("repetition_penalty", 1.1),
                    "depth_temperature": req.get("depth_temperature", 0.9),
                    "depth_top_k": req.get("depth_top_k", 50),
                    "depth_top_p": req.get("depth_top_p", 1.0),
                })
                if mode in {"clone", "direction"}:
                    reference_path = req.get("reference_path") or (LAB / "reference" / "af_heart.wav")
                    reference = self._resolve_reference_path(key, reference_path)
                    params["reference_path"] = str(reference)
                    params["reference_text"] = req.get("reference_text", "")
                    worker.call("prepare_reference", path=str(reference))
            elif key in QWEN_MODES:
                mode = QWEN_MODES[key]
                params.update({
                    "mode": mode,
                    "language": req.get("language", "English"),
                    "seed": req.get("seed", 42),
                    "max_new_tokens": req.get("max_new_tokens", 512),
                    "temperature": req.get("temperature", 0.9),
                    "top_k": req.get("top_k", 50),
                    "top_p": req.get("top_p", 1.0),
                    "repetition_penalty": req.get("repetition_penalty", 1.05),
                })
                if mode == "design":
                    params["voice_description"] = req.get("voice_description", "")
                elif mode == "clone":
                    reference = self._resolve_reference_path(key, req["reference_path"])
                    params.update({
                        "reference_path": str(reference),
                        "reference_text": req.get("reference_text", ""),
                        "xvec_only": req.get("xvec_only", False),
                        "direction": req.get("direction", ""),
                    })
                else:
                    params["speaker"] = req.get("speaker", "")
                    params["direction"] = req.get("direction", "")
            elif key == "voxcpm2":
                mode = req.get("mode", "design")
                params.update({
                    "mode": mode,
                    "seed": req.get("seed", 42),
                    "cfg_value": req.get("cfg_value", 2.0),
                    "inference_timesteps": req.get("inference_timesteps", 10),
                })
                if mode == "design":
                    params["voice_description"] = req.get("voice_description", "")
                elif mode == "clone":
                    reference = self._resolve_reference_path(key, req["reference_path"])
                    params["reference_path"] = str(reference)
                    params["direction"] = req.get("direction", "")
                    worker.call("prepare_reference", path=str(reference))
                else:
                    reference = self._resolve_reference_path(key, req["reference_path"])
                    prompt = self._resolve_reference_path(key, req["prompt_path"])
                    params.update({
                        "reference_path": str(reference), "prompt_path": str(prompt),
                        "prompt_text": req.get("prompt_text", ""),
                    })
                    worker.call("prepare_reference", path=str(reference))
            elif key == "turbo":
                params["clone"] = req.get("clone", True)
                params["sampling"] = chatterbox_sampling
                if params["clone"]:
                    reference_path = req.get("reference_path") or (LAB / "reference" / "af_heart.wav")
                    params["ref_wav"] = str(self._resolve_reference_path(key, reference_path))
                    worker.call("prepare_reference", path=params["ref_wav"])
            else:
                params["clone"] = bool(req.get("clone"))
                params["sampling"] = chatterbox_sampling
                params["exaggeration"] = chatterbox_sampling["exaggeration"]
                if params["clone"]:
                    reference_path = req.get("reference_path") or (LAB / "reference" / "af_heart.wav")
                    params["ref_wav"] = str(self._resolve_reference_path(key, reference_path))
                    worker.call("prepare_reference", path=params["ref_wav"],
                                exaggeration=params["exaggeration"])

            runtime_pid = self.workers[key].get("runtime_pid", worker.pid)
            worker_root = OUT / "workers" / f"{key}-{runtime_pid}"
            worker_root.mkdir(parents=True, exist_ok=True)
            output_file = worker_root / f"{uuid.uuid4().hex}.npy"
            result = worker.call("generate", output_file=str(output_file), **params)
            reported_path = Path(result.get("audio_file", "")).resolve()
            if reported_path != output_file.resolve() or not reported_path.is_file():
                raise WorkerUnavailable("Worker returned an invalid temporary audio path.")
            import numpy as np
            wav = np.load(reported_path, allow_pickle=False)
            if wav.ndim != 1 or wav.size == 0 or not np.isfinite(wav).all():
                raise WorkerRequestError("Worker returned invalid audio samples.")
            self.last_used = time.time()
            self.workers[key]["last_result"] = result
            self._last_error = None
            info = result.get("info") or {}
            self._record_clip(key, info, wav.size, int(result["sample_rate"]))
            return wav.astype(np.float32, copy=False), int(result["sample_rate"]), info
        except (WorkerUnavailable, WorkerTimeout):
            record = self.workers.get(key)
            if record:
                self._discard_locked(key, record["worker"])
            self._last_error = {"engine": key, "message": "worker exited or timed out; next request will restart it"}
            raise
        finally:
            if output_file is not None:
                try:
                    output_file.unlink(missing_ok=True)
                except OSError:
                    pass
            self._gpu_job.release()

    def stream_drama3(self, req, references, on_audio, cancel):
        """CPU production path, with owned-worker cancellation during load or prefill."""
        from drama3_production import script
        from drama3_runtime import controls
        script(req['text'])
        controls(req.get('settings'))
        refs=[{'path':str(self._resolve_reference_path('drama-3',ref['path'])),
               'text':ref['text'],'speaker':ref['speaker']} for ref in references]
        self._acquire_job()
        watching=threading.Event()
        def watch():
            while not watching.wait(.1):
                if cancel.is_set():
                    with self._state_lock:
                        owned=list(self._starting_workers)
                        record=self.workers.get('drama-3')
                        if record:owned.append(record['worker'])
                    for worker in owned:worker.close(force=True)
                    return
        monitor=threading.Thread(target=watch,daemon=True);monitor.start()
        try:
            if cancel.is_set():raise WorkerCancelled('Take stopped.')
            worker=self._ensure_loaded_locked('drama-3',{'cpu_only':True})
            worker.timeouts['generate_stream']=3600
            def chunk(packet):
                if cancel.is_set():return False
                on_audio(packet['pcm'],packet['sample_rate'])
                return True
            result=worker.call_stream('generate_stream',chunk,text=req['text'],references=refs,
                mode='cast' if refs else 'design',cpu_only=True,direction=req.get('direction',''),
                voice_description=req.get('description',''),**controls(req.get('settings')))
            return result
        finally:
            watching.set();monitor.join(6)
            try:self._unload_locked()
            finally:self._gpu_job.release()

    def stream_qwen(self, req, on_start, on_chunk):
        """Stream a faster-Qwen native generator through the existing serialized worker."""
        if not isinstance(req, dict):
            raise ValueError("Request body must be a JSON object.")
        key = req.get("engine", "")
        self._require_api_engine(key)
        if key not in QWEN_MODES:
            raise ValueError("Native streaming is currently available only for faster-Qwen checkpoints.")
        mode = QWEN_MODES[key]
        if req.get("mode", mode) != mode:
            raise ValueError(f"Checkpoint {key!r} supports mode {mode!r}; it does not support {req.get('mode')!r}.")
        text = req.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Type something to say.")
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters (split long replies into sentences).")
        common_fields = {"engine", "text", "mode", "language", "seed", "max_new_tokens", "save_voice_as",
                         "temperature", "top_k", "top_p", "repetition_penalty"}
        mode_fields = {
            "design": {"voice_description"},
            "clone": {"reference_path", "reference_text", "xvec_only", "direction"},
            "custom_voice": {"speaker", "direction"},
        }[mode]
        unsupported = set(req) - common_fields - mode_fields
        if unsupported:
            raise ValueError(f"Unsupported Qwen {mode} parameter(s): {', '.join(sorted(unsupported))}.")
        if mode == "design" and not str(req.get("voice_description", "")).strip():
            raise ValueError("Qwen VoiceDesign requires a non-empty voice_description.")
        if mode == "clone":
            if not req.get("reference_path"):
                raise ValueError("Qwen reference cloning requires reference_path.")
            if not isinstance(req.get("xvec_only", False), bool):
                raise ValueError("xvec_only must be true or false.")
            if not req.get("xvec_only", False) and not str(req.get("reference_text", "")).strip():
                raise ValueError("Qwen reference cloning requires reference_text unless xvec_only is enabled.")
        if mode == "custom_voice" and not str(req.get("speaker", "")).strip():
            raise ValueError("Qwen CustomVoice requires a speaker returned by the loaded checkpoint.")
        if req.get("save_voice_as") and mode != "design":
            raise ValueError("Only a Qwen VoiceDesign take can be saved as a new reusable voice reference.")
        if not callable(on_start) or not callable(on_chunk):
            raise TypeError("Streaming start and chunk callbacks must be callable.")

        params = {
            "text": text.strip(),
            "mode": mode,
            "language": req.get("language", "English"),
            "seed": req.get("seed", 42),
            "max_new_tokens": req.get("max_new_tokens", 512),
            "temperature": req.get("temperature", 0.9),
            "top_k": req.get("top_k", 50),
            "top_p": req.get("top_p", 1.0),
            "repetition_penalty": req.get("repetition_penalty", 1.05),
        }
        reference = None
        if mode == "design":
            params["voice_description"] = req.get("voice_description", "")
        elif mode == "clone":
            reference = self._resolve_reference_path(key, req["reference_path"])
            params.update({
                "reference_path": str(reference),
                "reference_text": req.get("reference_text", ""),
                "xvec_only": req.get("xvec_only", False),
                "direction": req.get("direction", ""),
            })
        else:
            params["speaker"] = req.get("speaker", "")
            params["direction"] = req.get("direction", "")

        self._acquire_job()
        try:
            self.last_used = time.time()
            worker = self._ensure_loaded_locked(key)
            load = self.workers[key].get("load_result") or {}
            on_start({"engine": key, "mode": mode, "sample_rate": int(load.get("sample_rate") or 24000),
                      "streaming_kind": "native_generator", "sample_format": "pcm_s16le"})
            result = worker.call_stream("generate_stream", on_chunk, **params)
            if reference is not None:
                result.setdefault("reference_path", str(reference))
            self.last_used = time.time()
            self.workers[key]["last_result"] = result
            self._last_error = None
            self._record_clip(key, result, result.get("sample_count"), result.get("sample_rate"))
            return result
        except WorkerCancelled as exc:
            record = self.workers.get(key)
            if record:
                self._discard_locked(key, record["worker"])
            self._last_error = {"engine": key, "message": "stream cancelled; next request will restart the worker"}
            raise exc
        except (WorkerUnavailable, WorkerTimeout):
            record = self.workers.get(key)
            if record:
                self._discard_locked(key, record["worker"])
            self._last_error = {"engine": key, "message": "worker exited or timed out; next request will restart it"}
            raise
        finally:
            self._gpu_job.release()

    def convert(self, source_path, target_reference_path):
        """Run the isolated Chatterbox VC task with distinct source and target assets."""
        key = "chatterbox-vc"
        self._require_api_engine(key)
        if str(source_path) == str(target_reference_path):
            raise ValueError("Source performance and target voice reference must be separate files.")
        self._acquire_job()
        output_file = None
        try:
            self.last_used = time.time()
            worker = self._ensure_loaded_locked(key)
            runtime_pid = self.workers[key].get("runtime_pid", worker.pid)
            worker_root = OUT / "workers" / f"{key}-{runtime_pid}"
            worker_root.mkdir(parents=True, exist_ok=True)
            output_file = worker_root / f"{uuid.uuid4().hex}.npy"
            result = worker.call("generate", source_path=str(source_path),
                                 target_reference_path=str(target_reference_path),
                                 output_file=str(output_file))
            reported_path = Path(result.get("audio_file", "")).resolve()
            if reported_path != output_file.resolve() or not reported_path.is_file():
                raise WorkerUnavailable("Voice conversion worker returned an invalid temporary audio path.")
            import numpy as np
            wav = np.load(reported_path, allow_pickle=False)
            if wav.ndim != 1 or wav.size == 0 or not np.isfinite(wav).all():
                raise WorkerRequestError("Voice conversion worker returned invalid audio samples.")
            self.last_used = time.time()
            self.workers[key]["last_result"] = result
            self._last_error = None
            info = result.get("info") or {}
            self._record_clip(key, info, wav.size, int(result["sample_rate"]))
            return wav.astype(np.float32, copy=False), int(result["sample_rate"]), info
        except (WorkerUnavailable, WorkerTimeout):
            record = self.workers.get(key)
            if record:
                self._discard_locked(key, record["worker"])
            self._last_error = {"engine": key, "message": "worker exited or timed out; next request will restart it"}
            raise
        finally:
            if output_file is not None:
                try:
                    output_file.unlink(missing_ok=True)
                except OSError:
                    pass
            self._gpu_job.release()

    def _unload_locked(self, release_ownership=True):
        with self._state_lock:
            current = list(self.workers.items())
        unloaded = []
        for key, record in current:
            worker = record["worker"]
            result = None
            try:
                if worker.alive and record["loaded"]:
                    result = worker.call("unload")
                    record["loaded"] = False
                worker.close(force=False)
            except Exception:
                worker.close(force=True)
            runtime_pid = record.get("runtime_pid", worker.pid)
            unloaded.append({"engine": key, "pid": runtime_pid, "launcher_pid": worker.pid,
                             "process_alive": worker.alive, "runtime_process_alive": process_is_running(runtime_pid),
                             "memory_release": result})
            if worker.alive or process_is_running(runtime_pid):
                self._last_unload = unloaded
                raise WorkerUnavailable(f"Worker {key} did not stop; cannot admit another model while its process is alive.")
            drop_scratch(key, runtime_pid)
            with self._state_lock:
                if self.workers.get(key) is record:
                    del self.workers[key]
        self._last_unload = unloaded
        if release_ownership and self._residency:
            self._residency.release()

    def unload(self):
        self._acquire_job()
        try:
            self._unload_locked()
            self.last_used = time.time()
        finally:
            self._gpu_job.release()

    def health(self):
        with self._state_lock:
            workers = []
            for key, record in self.workers.items():
                worker = record["worker"]
                last = record.get("last_result") or {}
                runtime_pid = record.get("runtime_pid", worker.pid)
                workers.append({"engine": key, "pid": runtime_pid, "launcher_pid": worker.pid,
                                "process_alive": worker.alive,
                                "runtime_process_alive": process_is_running(runtime_pid),
                                "model_loaded": bool(record["loaded"] and worker.alive),
                                "profile": record.get("profile"),
                                "load_seconds": (record.get("load_result") or {}).get("load_seconds"),
                                "memory": last.get("memory", {}),
                                "model_info": {key: value for key, value in (record.get("load_result") or {}).items()
                                               if key in {"backend", "checkpoint", "checkpoint_revision", "mode",
                                                          "supported_speakers", "supported_languages", "runtime_identity"}},
                                "log_path": str(worker.log_path)})
        return {"status": "busy" if self.busy else "ready", "busy": self.busy,
                "device": self.device, "workers": sorted(workers, key=lambda item: item["engine"]),
                "last_worker_error": self._last_error, "last_unload": self._last_unload}

    def _reap_idle(self):
        while not self._closed.wait(self.idle_poll_seconds):
            if not self.idle or time.time() - self.last_used <= self.idle:
                continue
            if not self._gpu_job.acquire(blocking=False):
                continue
            try:
                if time.time() - self.last_used > self.idle and self.workers:
                    print("Idle: unloading and stopping voice model workers.", flush=True)
                    try:
                        self._unload_locked()
                    except WorkerError as exc:
                        self._last_error = {"message": str(exc)}
            finally:
                self._gpu_job.release()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        with self._state_lock:
            workers = [record["worker"] for record in self.workers.values()]
            workers.extend(self._starting_workers)
            scratch = [(key, record.get("runtime_pid", record["worker"].pid)) for key, record in self.workers.items()]
            self.workers.clear()
            self._starting_workers.clear()
        # Force-stop only Popen handles created and owned by this pool, even during startup or a long request.
        for worker in set(workers):
            worker.close(force=True)
        for key, pid in scratch:
            drop_scratch(key, pid)
        if self._residency:
            self._residency.release()
