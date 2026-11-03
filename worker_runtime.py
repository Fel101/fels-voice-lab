"""Model lifecycle and generation methods executed only inside a model worker process."""

import os
import time
from importlib.util import find_spec
from pathlib import Path

import numpy as np

from common import LAB, free_ram_gb, release_gpu, require_ram, strip_tags, to_numpy, torch_runtime
from phonon_support import PHONON_ENGINE_ID
from reference_paths import resolve_reference_path

QWEN_ENGINE_IDS = {
    "qwen3-tts-1.7b-voice-design", "qwen3-tts-1.7b-base",
    "qwen3-tts-0.6b-base", "qwen3-tts-1.7b-custom-voice",
}
VOXCPM_ENGINE_ID = "voxcpm2"
CHATTERBOX_VC_ENGINE_ID = "chatterbox-vc"
ENGINE_IDS = {"kokoro", PHONON_ENGINE_ID, "turbo", "orig", "breeze-tts-2", VOXCPM_ENGINE_ID,
              CHATTERBOX_VC_ENGINE_ID, 'drama-3', *QWEN_ENGINE_IDS}
REFERENCE_DIR = (LAB / "reference").resolve()
DEFAULT_REFERENCE = REFERENCE_DIR / "af_heart.wav"
# Phonon peaked at about 0.6 GB in use, so 2 GB free is a wide margin. Chatterbox Turbo on the CPU peaked at 6.1 GB while it
# loaded and held 5.0 GB after a line of 1,200 characters, so 8 GB free leaves about 2 GB to spare.
MIN_FREE_RAM_GB = {"kokoro": 8, PHONON_ENGINE_ID: 2, "turbo": 8, "orig": 10, "breeze-tts-2": 12,
                   VOXCPM_ENGINE_ID: 10, CHATTERBOX_VC_ENGINE_ID: 10, 'drama-3':26,
                   **{key: 10 for key in QWEN_ENGINE_IDS}}


class EngineWorkerRuntime:
    def __init__(self, engine_id, device):
        if engine_id not in ENGINE_IDS:
            raise ValueError(f"Engine {engine_id!r} is not enabled for the worker service.")
        self.engine_id = engine_id
        self.device = device
        self.engine = None
        self.breeze = None
        self.qwen = None
        self.voxcpm = None
        self.chatterbox_vc = None
        self.drama3 = None
        self.model_info = None
        self.profile = None
        self.sample_rate = None
        self.loaded_at = None
        self.last_memory = self._memory()

    def _memory(self):
        result = {"allocated_bytes": 0, "reserved_bytes": 0}
        try:
            torch = torch_runtime()
            if torch.cuda.is_available():
                result["allocated_bytes"] = int(torch.cuda.memory_allocated())
                result["reserved_bytes"] = int(torch.cuda.memory_reserved())
                result["device_name"] = torch.cuda.get_device_name()
        except ModuleNotFoundError as exc:
            # Phonon runs without PyTorch, so this is how its worker normally reports memory.
            result["memory_note"] = ("PyTorch is not installed here, so GPU memory is not tracked." if exc.name == "torch"
                                     else f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            result["memory_note"] = f"{type(exc).__name__}: {exc}"
        return result

    def health(self):
        memory = self._memory()
        return {
            "engine_id": self.engine_id,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "model_loaded": self.engine is not None,
            "profile": self.profile,
            "model_info": self.model_info,
            "device": self.device,
            "free_ram_gb": round(free_ram_gb(), 2),
            "memory": memory,
        }

    def load(self, params=None):
        params = params or {}
        if self.engine is not None:
            if self.engine_id == "breeze-tts-2" and params.get("profile", self.profile) != self.profile:
                raise ValueError(f"Breeze worker is loaded with profile {self.profile!r}; unload it before changing profiles.")
            return {"already_loaded": True, **self.health()}
        if self.engine_id == 'drama-3':
            from drama3_worker import Drama3Worker
            require_ram(26, 'drama-3')
            self.drama3 = Drama3Worker('cpu' if params.get('cpu_only') else self.device)
            result = self.drama3.load()
            self.engine = self.drama3
            self.sample_rate = 44100
            self.loaded_at = time.time()
            self.model_info = result
            return {**result, 'pid':os.getpid(), 'parent_pid':os.getppid()}
        if self.engine_id == "breeze-tts-2":
            require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)
            from breeze_worker import BreezeWorker
            if self.breeze is None:
                self.breeze = BreezeWorker(self.device)
            result = self.breeze.load(params.get("profile", "eager"))
            self.engine = self.breeze.bundle
            self.sample_rate = int(result["sample_rate"])
            self.profile = result["profile"]
            self.loaded_at = time.time()
            self.last_memory = self._memory()
            return {**result, "pid": os.getpid(), "parent_pid": os.getppid(), "memory": self.last_memory}
        if self.engine_id in QWEN_ENGINE_IDS:
            require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)
            from qwen_worker import QwenWorker
            if self.qwen is None:
                self.qwen = QwenWorker(self.engine_id, self.device)
            started = time.perf_counter()
            result = self.qwen.load()
            self.engine = self.qwen.model
            self.sample_rate = int(result["sample_rate"])
            self.loaded_at = time.time()
            self.model_info = {key: result[key] for key in
                               ("backend", "backend_version", "checkpoint", "checkpoint_revision", "mode",
                                "runtime_identity", "supported_speakers", "supported_languages", "dtype", "attention")
                               if key in result}
            self.last_memory = self._memory()
            return {**result, "pid": os.getpid(), "parent_pid": os.getppid(),
                    "memory": self.last_memory,
                    "load_seconds": result.get("load_seconds", round(time.perf_counter() - started, 3))}
        if self.engine_id == VOXCPM_ENGINE_ID:
            require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)
            from voxcpm_worker import VoxCPMWorker
            if self.voxcpm is None:
                self.voxcpm = VoxCPMWorker(self.device)
            started = time.perf_counter()
            result = self.voxcpm.load()
            self.engine = self.voxcpm.model
            self.sample_rate = int(result["sample_rate"])
            self.loaded_at = time.time()
            self.model_info = {key: result[key] for key in
                               ("backend", "backend_version", "checkpoint", "checkpoint_revision",
                                "mode", "runtime_identity", "dtype", "device_name", "torch", "hip")
                               if key in result}
            self.last_memory = self._memory()
            return {**result, "pid": os.getpid(), "parent_pid": os.getppid(),
                    "memory": self.last_memory,
                    "load_seconds": result.get("load_seconds", round(time.perf_counter() - started, 3))}
        if self.engine_id == CHATTERBOX_VC_ENGINE_ID:
            require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)
            from chatterbox_vc_worker import ChatterboxVCWorker
            if self.chatterbox_vc is None:
                self.chatterbox_vc = ChatterboxVCWorker(self.device)
            started = time.perf_counter()
            result = self.chatterbox_vc.load()
            self.engine = self.chatterbox_vc.model
            self.sample_rate = int(result["sample_rate"])
            self.loaded_at = time.time()
            self.model_info = {key: result[key] for key in
                               ("backend", "backend_version", "checkpoint", "checkpoint_revision",
                                "mode", "runtime_identity", "device_name", "torch", "hip")
                               if key in result}
            self.last_memory = self._memory()
            return {**result, "pid": os.getpid(), "parent_pid": os.getppid(),
                    "memory": self.last_memory,
                    "load_seconds": result.get("load_seconds", round(time.perf_counter() - started, 3))}
        if (self.engine_id not in {"kokoro", PHONON_ENGINE_ID} and not DEFAULT_REFERENCE.is_file()
                and find_spec("kokoro") is not None):
            # Keep the existing service's synthetic fallback behavior. This creates no user recording. Chatterbox runs in
            # .venv-chatterbox, which has no Kokoro, so there it warms up with its built-in voice instead.
            from engines import ensure_reference
            ensure_reference()
        require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)
        from engines import build_engine

        started = time.perf_counter()
        print(f"Loading {self.engine_id} on {self.device} (worker {os.getpid()}) ...", flush=True)
        model = build_engine(self.engine_id, self.device)
        self.engine = model
        self.sample_rate = int(model.sr)
        print("Warming up the model ...", flush=True)
        try:
            if self.engine_id in {"kokoro", PHONON_ENGINE_ID} or not DEFAULT_REFERENCE.is_file():
                model.speak("Hello there.")
            elif self.engine_id == "orig":
                model.prepare_reference(str(DEFAULT_REFERENCE), exaggeration=0.5)
                model.speak("Hello there.", ref_wav=DEFAULT_REFERENCE)
            else:
                model.prepare_reference(str(DEFAULT_REFERENCE))
                model.speak("Hello there.", ref_wav=DEFAULT_REFERENCE)
        except Exception as exc:
            print(f"Warm-up failed: {type(exc).__name__}: {exc}", flush=True)
        self.loaded_at = time.time()
        self.last_memory = self._memory()
        if self.engine_id == PHONON_ENGINE_ID:
            checkpoint, revision = model.pins["model"]
            self.model_info = {"backend": "ptts", "backend_version": model.version, "checkpoint": checkpoint,
                               "checkpoint_revision": revision, "supported_speakers": list(model.voices),
                               "supported_languages": ["English"]}
        return {
            "already_loaded": False,
            "engine_id": self.engine_id,
            "model_loaded": True,
            "device": model.device if self.engine_id == PHONON_ENGINE_ID else self.device,
            "load_seconds": round(time.perf_counter() - started, 3),
            "sample_rate": self.sample_rate,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "memory": self.last_memory,
            **(self.model_info if self.engine_id == PHONON_ENGINE_ID else {}),
        }

    def prepare_reference(self, params):
        if self.engine is None:
            raise RuntimeError("Load the model before preparing a reference.")
        if self.engine_id == 'drama-3':
            return self.drama3.prepare_reference(params)
        if self.engine_id == "kokoro":
            raise ValueError("Kokoro uses fixed voice IDs and does not accept reference audio.")
        if self.engine_id == PHONON_ENGINE_ID:
            raise ValueError("Phonon uses its preset voices and does not accept reference audio.")
        path = Path(params.get("path", "")).expanduser().resolve()
        if self.engine_id == "breeze-tts-2":
            return self.breeze.prepare_reference(str(path))
        if self.engine_id in QWEN_ENGINE_IDS:
            return self.qwen.prepare_reference(params)
        if self.engine_id == VOXCPM_ENGINE_ID:
            return self.voxcpm.prepare_reference(params)
        path = resolve_reference_path(self.engine_id, path)
        exaggeration = float(params.get("exaggeration", 0.5))
        if self.engine_id == "orig":
            self.engine.prepare_reference(str(path), exaggeration=exaggeration)
        else:
            self.engine.prepare_reference(str(path))
        return {"prepared": True, "engine_id": self.engine_id, "path": str(path)}

    def generate(self, params):
        if self.engine is None:
            raise RuntimeError("Load the model before generation.")
        text = params.get("text", "Voice conversion" if self.engine_id == CHATTERBOX_VC_ENGINE_ID else None)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string.")
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters (split long replies into sentences).")

        if self.engine_id == 'drama-3':
            wav, sr, info = self.drama3.generate(params)
        elif self.engine_id == CHATTERBOX_VC_ENGINE_ID:
            wav, sr, info = self.chatterbox_vc.generate(params)
        elif self.engine_id == "breeze-tts-2":
            wav, sr, info = self.breeze.generate(params)
        elif self.engine_id in QWEN_ENGINE_IDS:
            # output_file belongs to the broker/worker audio handoff protocol;
            # it is not a user generation kwarg for the strict Qwen adapter.
            qwen_params = {key: value for key, value in params.items() if key != "output_file"}
            wav, sr, info = self.qwen.generate(qwen_params)
        elif self.engine_id == VOXCPM_ENGINE_ID:
            voxcpm_params = {key: value for key, value in params.items() if key != "output_file"}
            wav, sr, info = self.voxcpm.generate(voxcpm_params)
        elif self.engine_id == PHONON_ENGINE_ID:
            wav, sr, info = self.engine.speak(strip_tags(text.strip()), voice=params.get("voice"),
                                              seed=params.get("seed", 42), temperature=params.get("temperature", 0.3),
                                              duration_delta=params.get("duration_delta", 0.0))
        elif self.engine_id == "kokoro":
            voice = str(params.get("voice", "af_heart"))
            speed = min(2.0, max(0.5, float(params.get("speed", 1.0))))
            wav, sr, info = self.engine.speak(strip_tags(text.strip()), voice=voice, speed=speed,
                                              language=params.get("language", "English"))
        elif self.engine_id == "turbo":
            clone = params.get("clone", True)
            ref_wav = str(params.get("ref_wav") or DEFAULT_REFERENCE) if clone else None
            if ref_wav:
                self.prepare_reference({"path": ref_wav})
            wav, sr, info = self.engine.speak(text.strip(), ref_wav=ref_wav,
                                               **(params.get("sampling") or {}))
        else:
            clone = bool(params.get("clone", False))
            ref_wav = str(params.get("ref_wav") or DEFAULT_REFERENCE) if clone else None
            sampling = dict(params.get("sampling") or {})
            exaggeration = float(sampling.get("exaggeration", 0.5))
            cfg_weight = float(sampling.get("cfg_weight", 0.5))
            if ref_wav:
                self.prepare_reference({"path": ref_wav, "exaggeration": exaggeration})
            wav, sr, info = self.engine.speak(strip_tags(text.strip()), ref_wav=ref_wav,
                                               exaggeration=exaggeration, cfg_weight=cfg_weight,
                                               **{key: value for key, value in sampling.items()
                                                  if key not in {"exaggeration", "cfg_weight"}})

        audio = to_numpy(wav)
        if audio.size == 0 or not np.isfinite(audio).all():
            raise RuntimeError("Engine produced empty or non-finite audio.")
        output_file = Path(params.get("output_file", "")).resolve()
        allowed_root = (LAB / "out" / "workers" / f"{self.engine_id}-{os.getpid()}").resolve()
        if output_file.parent != allowed_root or output_file.suffix != ".npy":
            raise ValueError(f"Audio output path is outside the worker's temporary output directory: "
                             f"output_parent={output_file.parent}; expected={allowed_root}; suffix={output_file.suffix!r}.")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        np.save(output_file, audio, allow_pickle=False)
        self.last_memory = self._memory()
        return {
            "audio_file": str(output_file),
            "sample_rate": int(sr),
            "sample_count": int(audio.size),
            "info": info,
            "memory": self.last_memory,
        }

    def generate_stream(self, params, emit_audio):
        if self.engine is None:
            raise RuntimeError("Load the model before generation.")
        if self.engine_id == 'drama-3':
            def emit(pcm,rate):
                emit_audio(np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32767.,rate)
            audio,rate,info=self.drama3.generate(params,on_audio=emit)
            return {**info,'sample_count':len(audio),'sample_rate':rate,'pid':os.getpid()}
        if self.engine_id not in QWEN_ENGINE_IDS:
            raise ValueError("Native worker streaming is currently available only for faster-Qwen checkpoints.")
        result = self.qwen.generate_stream(params, emit_audio)
        self.last_memory = self._memory()
        return {**result, "memory": self.last_memory, "pid": os.getpid(), "parent_pid": os.getppid()}

    def unload(self):
        before = self._memory()
        if self.drama3 is not None:
            self.drama3.unload()
            self.drama3 = None
        breeze_result = None
        qwen_result = None
        voxcpm_result = None
        chatterbox_vc_result = None
        if self.engine_id == "breeze-tts-2" and self.breeze is not None:
            breeze_result = self.breeze.unload()
        if self.engine_id in QWEN_ENGINE_IDS and self.qwen is not None:
            qwen_result = self.qwen.unload()
        if self.engine_id == VOXCPM_ENGINE_ID and self.voxcpm is not None:
            voxcpm_result = self.voxcpm.unload()
        if self.engine_id == CHATTERBOX_VC_ENGINE_ID and self.chatterbox_vc is not None:
            chatterbox_vc_result = self.chatterbox_vc.unload()
        self.engine = None
        self.sample_rate = None
        self.loaded_at = None
        self.profile = None
        self.model_info = None
        release_gpu()
        after = self._memory()
        self.last_memory = after
        return {
            "model_loaded": False,
            "memory_before": before,
            "memory_after": after,
            "allocated_bytes_released": max(0, before.get("allocated_bytes", 0) - after.get("allocated_bytes", 0)),
            **({"breeze": breeze_result} if breeze_result is not None else {}),
            **({"qwen": qwen_result} if qwen_result is not None else {}),
            **({"voxcpm2": voxcpm_result} if voxcpm_result is not None else {}),
            **({"chatterbox_vc": chatterbox_vc_result} if chatterbox_vc_result is not None else {}),
        }

    def dispatch(self, method, params, *, emit_audio=None):
        if method == "health":
            return self.health(), False
        if method == "load":
            return self.load(params), False
        if method == "prepare_reference":
            return self.prepare_reference(params), False
        if method == "generate":
            return self.generate(params), False
        if method == "generate_stream":
            if emit_audio is None:
                raise ValueError("Worker stream method requires an audio chunk transport.")
            return self.generate_stream(params, emit_audio), False
        if method == "unload":
            return self.unload(), False
        if method == "shutdown":
            result = self.unload()
            return {"stopped": True, **result}, True
        raise ValueError(f"Unknown worker method {method!r}.")
