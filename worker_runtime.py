"""Model lifecycle and generation methods executed only inside a model worker process."""

import os
import time
from pathlib import Path

import numpy as np

import engine_adapters
from common import LAB, free_ram_gb, release_gpu, require_ram, to_numpy, torch_runtime
from ram_needs import MIN_FREE_RAM_GB

ENGINE_IDS = {'drama-3', *engine_adapters.ADAPTERS}
# The engines that run on PyTorch: an adapter says when its engine does not (Phonon's ptts), and Drama (speech.cpp) chooses
# its own device.
TORCH_ENGINE_IDS = ENGINE_IDS - {"drama-3"} - {engine for engine, adapter in engine_adapters.ADAPTERS.items()
                                               if not adapter.uses_torch}
REFERENCE_DIR = (LAB / "reference").resolve()
DEFAULT_REFERENCE = REFERENCE_DIR / "af_heart.wav"


def runs_on(capability, arch_list):
    """Whether PyTorch built for these architectures (its get_arch_list(), such as ["sm_80", "sm_86", "sm_90", "compute_90"])
    has code for a card of this compute capability: code built for the same generation and no newer, or PTX of the same or
    an older one, which the driver compiles for the card."""
    major, minor = capability
    for arch in arch_list:
        kind, _, digits = str(arch).partition("_")
        if not digits.isdigit() or len(digits) < 2:
            continue
        built = (int(digits[:-1]), int(digits[-1]))
        if (kind == "sm" and built[0] == major and built[1] <= minor) or (kind == "compute" and built <= (major, minor)):
            return True
    return False


def missing_cuda_code(torch):
    """Why this CUDA build of PyTorch cannot run on the graphics card, or None when it can or cannot tell (a ROCm build for an
    AMD card answers to "cuda" too, and is left to PyTorch). PyTorch 2.6, for one, reports an RTX 50 series card as available
    and then has no code to run on it."""
    version = getattr(torch, "version", None)
    if version is None or getattr(version, "hip", None) or not getattr(version, "cuda", None):
        return None
    try:
        capability = tuple(torch.cuda.get_device_capability(0))
        arch_list = list(torch.cuda.get_arch_list())
        name = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001 - any failure to read the card means it cannot be used
        return f"this environment's PyTorch ({torch.__version__}) could not read the graphics card ({type(exc).__name__}: {exc})"
    if not arch_list or runs_on(capability, arch_list):
        return None
    return (f"this environment's PyTorch ({torch.__version__}) has no code for the {name} (compute capability "
            f"{capability[0]}.{capability[1]})")


def usable_device(engine_id, device):
    """(device, note): the device this worker can really use. Asked for the graphics card, an engine whose PyTorch cannot use
    one runs on the CPU instead of failing to load, and the note says why: the CPU build the installers put in unless told
    --nvidia, or a CUDA build with no code for this card. Any other request is kept as it is."""
    if device != "cuda" or engine_id not in TORCH_ENGINE_IDS:
        return device, None
    try:
        torch = torch_runtime()
    except ModuleNotFoundError:
        return "cpu", "PyTorch is not installed in this environment"
    if not torch.cuda.is_available():
        return "cpu", f"this environment's PyTorch ({torch.__version__}) cannot use a graphics card"
    missing = missing_cuda_code(torch)
    return ("cpu", missing) if missing else ("cuda", None)


class EngineWorkerRuntime:
    def __init__(self, engine_id, device):
        if engine_id not in ENGINE_IDS:
            raise ValueError(f"Engine {engine_id!r} is not enabled for the worker service.")
        self.engine_id = engine_id
        # The engine's adapter (engine_adapters/) does its load, clips and references; None for an engine still handled here.
        self.adapter = engine_adapters.get(engine_id)
        self.device, self.device_note = usable_device(engine_id, device)
        if self.device_note:
            print(f"The service asked for the graphics card, but {self.device_note}, so {engine_id} runs on the CPU.",
                  flush=True)
        self.engine = None
        self.backend = None  # the worker object an adapter keeps across loads (engine_adapters/)
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
            **({"device_note": self.device_note} if self.device_note else {}),
            "free_ram_gb": round(free_ram_gb(), 2),
            "memory": memory,
        }

    def load(self, params=None):
        params = params or {}
        if self.engine is not None:
            if getattr(self.adapter, "profiles", False) and params.get("profile", self.profile) != self.profile:
                raise ValueError(f"Breeze worker is loaded with profile {self.profile!r}; unload it before changing profiles.")
            return {"already_loaded": True, **self.health()}
        if self.adapter is not None:
            require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)
            return self.adapter.worker_load(self, params)
        if self.engine_id == 'drama-3':
            from drama3_worker import Drama3Worker
            require_ram(MIN_FREE_RAM_GB['drama-3'], 'drama-3')
            self.drama3 = Drama3Worker('cpu' if params.get('cpu_only') else self.device)
            result = self.drama3.load()
            self.engine = self.drama3
            self.sample_rate = 44100
            self.loaded_at = time.time()
            self.model_info = result
            return {**result, 'pid':os.getpid(), 'parent_pid':os.getppid()}
        raise ValueError(f"{self.engine_id} has no worker.")

    @staticmethod
    def _make_default_reference(model):
        """Speak reference/af_heart.wav, the synthetic clip the cloning models' checks clone, with the Kokoro model just loaded.
        A failure is printed and the load goes on: the clip is a convenience, and Kokoro itself does not need it."""
        from engines import ensure_reference
        from kokoro_support import discover_kokoro_voices
        if "af_heart" not in discover_kokoro_voices():
            return
        try:
            ensure_reference(model)
        except Exception as exc:
            print(f"The default reference clip was not made: {type(exc).__name__}: {exc}", flush=True)

    def phonemize(self, params):
        """The phonemes Kokoro would speak for a text, from its installed language rules. Needs no loaded model."""
        if not getattr(self.adapter, "phonemizes", False):
            raise ValueError("Only Kokoro shows its phonemes.")
        return self.adapter.worker_phonemize(self, params)

    def prepare_reference(self, params):
        if self.engine is None:
            raise RuntimeError("Load the model before preparing a reference.")
        if self.adapter is not None:
            return self.adapter.worker_prepare_reference(self, params)
        if self.engine_id == 'drama-3':
            return self.drama3.prepare_reference(params)
        raise ValueError(f"{self.engine_id} does not take a voice recording.")

    def generate(self, params):
        if self.engine is None:
            raise RuntimeError("Load the model before generation.")
        text = params.get("text", getattr(self.adapter, "default_text", None))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string.")
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters (split long replies into sentences).")

        if self.adapter is not None:
            wav, sr, info = self.adapter.worker_generate(self, text, params)
        elif self.engine_id == 'drama-3':
            wav, sr, info = self.drama3.generate(params)

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
        if isinstance(info, dict) and "device" not in info:
            # The clip's device, which the speed table and the Models page record; it may be the CPU after a fallback.
            info = {**info, "device": self.device}
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
        if not getattr(self.adapter, "streams", False):
            raise ValueError("Native worker streaming is currently available only for faster-Qwen checkpoints.")
        result = self.adapter.worker_generate_stream(self, params, emit_audio)
        self.last_memory = self._memory()
        return {**result, "memory": self.last_memory, "pid": os.getpid(), "parent_pid": os.getppid()}

    def unload(self):
        before = self._memory()
        if self.drama3 is not None:
            self.drama3.unload()
            self.drama3 = None
        adapter_result = self.adapter.worker_unload(self) if self.adapter is not None else {}
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
            **adapter_result,
        }

    def dispatch(self, method, params, *, emit_audio=None):
        if method == "health":
            return self.health(), False
        if method == "load":
            return self.load(params), False
        if method == "prepare_reference":
            return self.prepare_reference(params), False
        if method == "phonemize":
            return self.phonemize(params), False
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
