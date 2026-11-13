"""Chatterbox: Turbo (350M) and Original (500M) clone a voice recording, and VC converts a performance into another voice,
all through the chatterbox-tts package in .venv-chatterbox.

chatterbox_support.py holds their pinned files and sampling rules; engines.ChatterboxTurbo and engines.ChatterboxOriginal
are the speaking models in their worker, and chatterbox_vc_worker.py the converter."""

import os
import time
from pathlib import Path

import chatterbox_support as cs
from common import LAB, strip_tags
from engine_adapters.base import Adapter

SAMPLING_FIELDS = set().union(*cs.CHATTERBOX_SAMPLING_FIELDS.values())
# Turbo refuses a voice recording of this many seconds or fewer.
TURBO_MIN_SECONDS = 5.0
TURBO_NEEDS_REFERENCE = (
    "Chatterbox Turbo clones a reference recording: give reference_path (a clip longer than 5 seconds), or "
    "set clone to false for its built-in voice. The default reference, reference/af_heart.wav, is not there."
)


def default_reference():
    """The bundled clip a clone request with no voice recording uses: reference/af_heart.wav in the Lab the pool serves."""
    import worker_pool

    return worker_pool.LAB / "reference" / "af_heart.wav"


class Chatterbox(Adapter):
    """What the three share."""

    required_packages = ("chatterbox-tts",)
    gpu_build = "tools.install_chatterbox"
    sizes_install = True
    manifest = "ORIGINAL_MANIFEST"  # the installer's manifest that lists its files
    output_license = {
        "code_license": "MIT Chatterbox runtime, separately installed.",
        "weights_license": "MIT upstream Chatterbox checkpoint.",
        "output_use": "Review reference/subject rights; retain native Perth watermark. No unrestricted-output clearance is "
        "implied.",
        "status": "unknown",
        "evidence": "THIRD_PARTY_NOTICES.md and upstream ResembleAI/chatterbox license.",
    }

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_chatterbox as ic

        files = files_bytes(ic.base.load_manifest(root / getattr(ic, self.manifest)))
        download, disk = ic.packages_mb(nvidia=nvidia, card=card)
        return download * 10**6 + files, disk * 10**6 + files

    # The worker's half, shared: a voice recording is prepared once before it is spoken in.

    def worker_prepare_reference(self, runtime, params):
        from reference_paths import resolve_reference_path

        path = resolve_reference_path(runtime.engine_id, Path(params.get("path", "")).expanduser().resolve())
        self.prepare(runtime.engine, path, params)
        return {"prepared": True, "engine_id": runtime.engine_id, "path": str(path)}

    def prepare(self, model, path, params):
        model.prepare_reference(str(path))


class ChatterboxSpeaker(Chatterbox):
    """Turbo and Original: they speak text in a voice recording's voice, or in the checkpoint's own voice."""

    reference_voices = True
    languages = frozenset({"English"})
    reference_policy = {
        "max_seconds": 60.0,
        "limit_basis": "voice-lab safety ceiling; no stricter local native limit was established",
        "recommended_rate": 24_000,
        "requires_transcript_for_cast": True,
    }
    # A take's settings where the line sets none, before the sampling rules settle them.
    take_defaults = {}

    @property
    def performance_defaults(self):
        return cs.normalize_chatterbox_sampling(self.engine_id, self.take_defaults)

    def settle_settings(self, settings):
        return cs.normalize_chatterbox_sampling(self.engine_id, settings)

    def check_speak(self, req, pool):
        return cs.normalize_chatterbox_sampling(self.engine_id, {name: req[name] for name in SAMPLING_FIELDS if name in req})

    def performance_request(self, identity, settings, applied, direction, language):
        sampling = {key: settings[key] for key in cs.CHATTERBOX_SAMPLING_FIELDS[self.engine_id] if key in settings}
        return {"clone": True, **cs.normalize_chatterbox_sampling(self.engine_id, {**self.take_defaults, **sampling})}

    # The worker's half: engines.ChatterboxTurbo and engines.ChatterboxOriginal in .venv-chatterbox

    def worker_load(self, runtime, params):
        from importlib.util import find_spec

        from engines import build_engine
        from worker_runtime import DEFAULT_REFERENCE as bundled

        if not bundled.is_file() and find_spec("kokoro") is not None:
            # An environment you built yourself may hold Kokoro beside Chatterbox; the guided ones never do (Kokoro has
            # .venv-kokoro to itself), so there the Kokoro worker makes the clip and this model warms up with its own voice.
            from engines import ensure_reference

            ensure_reference()
        started = time.perf_counter()
        print(f"Loading {runtime.engine_id} on {runtime.device} (worker {os.getpid()}) ...", flush=True)
        model = build_engine(runtime.engine_id, runtime.device)
        runtime.engine = model
        runtime.sample_rate = int(model.sr)
        print("Warming up the model ...", flush=True)
        try:
            if not bundled.is_file():
                model.speak("Hello there.")
            else:
                self.warm_up(model, bundled)
        except Exception as exc:
            print(f"Warm-up failed: {type(exc).__name__}: {exc}", flush=True)
        runtime.loaded_at = time.time()
        runtime.last_memory = runtime._memory()
        return {
            "already_loaded": False,
            "engine_id": runtime.engine_id,
            "model_loaded": True,
            "device": runtime.device,
            "load_seconds": round(time.perf_counter() - started, 3),
            "sample_rate": runtime.sample_rate,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "memory": runtime.last_memory,
        }

    def warm_up(self, model, reference):
        model.prepare_reference(str(reference))
        model.speak("Hello there.", ref_wav=reference)


class ChatterboxTurbo(ChatterboxSpeaker):
    engine_id = cs.TURBO_ENGINE_ID
    guided_installer = "setup-chatterbox.bat"
    installer = "tools/install_chatterbox.py"
    manifest = "MANIFEST"
    required_files = cs.TURBO_FILES
    # Measured on the CPU, one worker speaking a long line: Turbo peaked at 6.1 GB while it loaded and held 5.0 GB after
    # 1,200 characters, so 8 GB leaves about 2 GB to spare.
    min_free_ram_gb = 8
    acted_markup = "square"  # it acts [laugh], [chuckle] and [cough] instead of saying them (word_timing.py)
    control_ui = {
        "temperature": ("Temperature", 0.05, 2.0, 0.05),
        "top_k": ("Top-k", 1, 2048, 1),
        "top_p": ("Top-p", 0.05, 1.0, 0.01),
        "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
    }

    def reference_problem(self, seconds):
        if seconds <= TURBO_MIN_SECONDS:
            return f"Turbo needs a reference longer than {TURBO_MIN_SECONDS:g} s (the clip is {seconds:.2f} s)"
        return None

    def check_reference(self, path):
        import soundfile as sf

        try:
            duration = sf.info(path).duration
        except (RuntimeError, OSError) as exc:
            raise ValueError("Turbo reference must be readable audio.") from exc
        if duration <= TURBO_MIN_SECONDS:
            raise ValueError(
                f"Turbo reference must be longer than 5 seconds (received {duration:.3f}s). Select or record a longer "
                "reference clip."
            )

    def check_speak(self, req, pool):
        sampling = super().check_speak(req, pool)
        if req.get("clone", True):
            reference = req.get("reference_path") or default_reference()
            if not (req.get("reference_path") or Path(reference).is_file()):
                # The worker cannot make the default clip: it runs in .venv-chatterbox, which has no Kokoro.
                raise ValueError(TURBO_NEEDS_REFERENCE)
            pool._resolve_reference_path(self.engine_id, reference)
        return sampling

    def speak_params(self, req, pool, worker, checked):
        params = {"clone": req.get("clone", True), "sampling": checked}
        if params["clone"]:
            reference_path = req.get("reference_path") or default_reference()
            params["ref_wav"] = str(pool._resolve_reference_path(self.engine_id, reference_path))
            worker.call("prepare_reference", path=params["ref_wav"])
        return params

    def smoke_request(self, base, loaded=None, root=None):
        if not (Path(root or LAB) / "reference" / "af_heart.wav").is_file():
            # Turbo clones the default reference when there is one (Kokoro makes it). Its own environment cannot make it, so
            # without it the check speaks in the built-in voice: the installer's speech check is the one that clones.
            return {**base, "clone": False}
        return base

    def worker_generate(self, runtime, text, params):
        from worker_runtime import DEFAULT_REFERENCE as bundled

        clone = params.get("clone", True)
        ref_wav = str(params.get("ref_wav") or bundled) if clone else None
        if ref_wav:
            runtime.prepare_reference({"path": ref_wav})
        return runtime.engine.speak(text.strip(), ref_wav=ref_wav, **(params.get("sampling") or {}))


class ChatterboxOriginal(ChatterboxSpeaker):
    engine_id = cs.ORIGINAL_ENGINE_ID
    guided_installer = "setup-chatterbox.bat --original"
    installer = ("tools/install_chatterbox.py", "--original")
    required_files = cs.ORIGINAL_FILES
    min_free_ram_gb = 10
    take_defaults = {"exaggeration": 0.6, "cfg_weight": 0.4}
    control_ui = {
        "exaggeration": ("Exaggeration", 0.0, 2.0, 0.05),
        "cfg_weight": ("CFG weight", 0.0, 1.0, 0.05),
        "temperature": ("Temperature", 0.05, 2.0, 0.05),
        "top_p": ("Top-p", 0.05, 1.0, 0.01),
        "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
        "min_p": ("Min-p", 0.0, 1.0, 0.01),
    }

    def speak_params(self, req, pool, worker, checked):
        params = {"clone": bool(req.get("clone")), "sampling": checked, "exaggeration": checked["exaggeration"]}
        if params["clone"]:
            reference_path = req.get("reference_path") or default_reference()
            params["ref_wav"] = str(pool._resolve_reference_path(self.engine_id, reference_path))
            worker.call("prepare_reference", path=params["ref_wav"], exaggeration=params["exaggeration"])
        return params

    def smoke_request(self, base, loaded=None, root=None):
        # Original uses its built-in voice, as a default call does.
        return base

    def warm_up(self, model, reference):
        model.prepare_reference(str(reference), exaggeration=0.5)
        model.speak("Hello there.", ref_wav=reference)

    def prepare(self, model, path, params):
        model.prepare_reference(str(path), exaggeration=float(params.get("exaggeration", 0.5)))

    def worker_generate(self, runtime, text, params):
        from worker_runtime import DEFAULT_REFERENCE as bundled

        clone = bool(params.get("clone", False))
        ref_wav = str(params.get("ref_wav") or bundled) if clone else None
        sampling = dict(params.get("sampling") or {})
        exaggeration = float(sampling.get("exaggeration", 0.5))
        cfg_weight = float(sampling.get("cfg_weight", 0.5))
        if ref_wav:
            runtime.prepare_reference({"path": ref_wav, "exaggeration": exaggeration})
        others = {key: value for key, value in sampling.items() if key not in {"exaggeration", "cfg_weight"}}
        return runtime.engine.speak(
            strip_tags(text.strip()), ref_wav=ref_wav, exaggeration=exaggeration, cfg_weight=cfg_weight, **others
        )


class ChatterboxVC(Chatterbox):
    engine_id = cs.VC_ENGINE_ID
    guided_installer = "setup-chatterbox.bat --original"
    installer = ("tools/install_chatterbox.py", "--original")
    required_files = cs.VC_FILES
    min_free_ram_gb = 10
    converts = True
    # A conversion has no words; the worker's checks still want some.
    default_text = "Voice conversion"

    def smoke_request(self, base, loaded=None, root=None):
        raise ValueError(f"No speech check is defined for {self.engine_id!r}.")

    # The worker's half: chatterbox_vc_worker.ChatterboxVCWorker in .venv-chatterbox

    def worker_load(self, runtime, params):
        from chatterbox_vc_worker import ChatterboxVCWorker

        if runtime.backend is None:
            runtime.backend = ChatterboxVCWorker(runtime.device)
        started = time.perf_counter()
        result = runtime.backend.load()
        runtime.engine = runtime.backend.model
        runtime.sample_rate = int(result["sample_rate"])
        runtime.loaded_at = time.time()
        kept = (
            "backend",
            "backend_version",
            "checkpoint",
            "checkpoint_revision",
            "mode",
            "runtime_identity",
            "device_name",
            "torch",
            "hip",
        )
        runtime.model_info = {key: result[key] for key in kept if key in result}
        runtime.last_memory = runtime._memory()
        return {
            **result,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "memory": runtime.last_memory,
            "load_seconds": result.get("load_seconds", round(time.perf_counter() - started, 3)),
        }

    def worker_generate(self, runtime, text, params):
        return runtime.backend.generate(params)

    def worker_unload(self, runtime):
        return {"chatterbox_vc": runtime.backend.unload()} if runtime.backend is not None else {}


ADAPTERS = {adapter.engine_id: adapter for adapter in (ChatterboxTurbo(), ChatterboxOriginal(), ChatterboxVC())}
