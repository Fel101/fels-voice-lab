"""Kokoro 82M: fixed voices with speed, in nine languages, through the kokoro package in .venv-kokoro.

kokoro_support.py reads the installed voices and languages and holds the phonemizer; engines.Kokoro is the model in its
worker. Kokoro's worker also makes reference/af_heart.wav, the synthetic clip the cloning models' checks clone."""

import os
import time

import kokoro_support
from common import strip_tags
from engine_adapters.base import Adapter

DEFAULT_VOICE = "af_heart"


class Kokoro(Adapter):
    engine_id = "kokoro"
    guided_installer = "setup-kokoro.bat"
    installer = "tools/install_kokoro.py"
    gpu_build = "tools.install_kokoro"
    required_packages = ("kokoro",)
    # Measured on the CPU, one worker speaking a long line: Kokoro held 1.2 GB once loaded and peaked at 1.8 GB (3.2 GB
    # committed) on 1,019 characters.
    min_free_ram_gb = 3
    sizes_install = True
    fixed_voices = True
    phonemizes = True
    # Its voices are its own, so it reads no voice recording but the bundled one (reference_paths.resolve_reference_path).
    takes_voice_recordings = False
    # Each voice speaks one language: the language matrix picks a voice for each language and names the column after it.
    voice_per_language = True
    languages = frozenset(
        {"English", "American English", "British English", "Spanish", "French", "Hindi", "Italian", "Brazilian Portuguese"}
    )
    performance_defaults = {"speed": 1.0}
    control_ui = {"speed": ("Speed", 0.5, 2.0, 0.05)}
    generation_mode = "fixed_voice"
    output_license = {
        "code_license": "Apache-2.0 Kokoro, separately installed.",
        "weights_license": "Apache-2.0 Kokoro-82M; preserve upstream model/voice notices when redistributing weights.",
        "output_use": "Unknown; review voice/reference rights before redistribution.",
        "status": "unknown",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json; complete upstream license.",
    }

    # The service's half

    def speak_params(self, req, pool, worker, checked):
        return {
            "voice": str(req.get("voice", DEFAULT_VOICE)),
            "speed": req.get("speed", 1.0),
            "language": req.get("language", "English"),
        }

    def performance_request(self, identity, settings, applied, direction, language):
        return {**identity["engine_options"], "speed": settings.get("speed", 1.0), "language": language}

    def take_record(self, voice_options):
        return {"fixed_voice": voice_options.get("voice", DEFAULT_VOICE)}

    def identity_options(self, capabilities, req, controls, pool):
        voices = kokoro_support.normalize_kokoro_voices(
            req.get("voice", DEFAULT_VOICE), kokoro_support.discover_kokoro_voices()
        )
        return {"voice": ",".join(voices)}

    def matrix_request(self, request, clip, loaded=None, root=None):
        return {**request, "voice": clip["voice"], "language": clip["label"]}

    def info(self):
        # Its installed voices are GET /api/info's "voices", which serve.py reads.
        return {"kokoro_languages": kokoro_support.kokoro_languages()}

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_kokoro as ik

        manifest = ik.load_manifest(root / ik.MANIFEST)
        files = files_bytes(manifest, ik.checkpoint_files(manifest, ik.choose_voices(manifest, ["supported"])))
        download, disk = ik.packages_mb(nvidia=nvidia, card=card)
        return download * 10**6 + files, disk * 10**6 + files

    # The worker's half: engines.Kokoro in .venv-kokoro

    def worker_load(self, runtime, params):
        from engines import build_engine
        from worker_runtime import DEFAULT_REFERENCE

        started = time.perf_counter()
        print(f"Loading {runtime.engine_id} on {runtime.device} (worker {os.getpid()}) ...", flush=True)
        model = build_engine(runtime.engine_id, runtime.device)
        runtime.engine = model
        runtime.sample_rate = int(model.sr)
        if not DEFAULT_REFERENCE.is_file():
            runtime._make_default_reference(model)
        print("Warming up the model ...", flush=True)
        try:
            model.speak("Hello there.")
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

    def worker_phonemize(self, runtime, params):
        from kokoro_support import phonemize_kokoro

        return phonemize_kokoro(params.get("text", ""), params.get("language", "English"))

    def worker_prepare_reference(self, runtime, params):
        raise ValueError("Kokoro uses fixed voice IDs and does not accept reference audio.")

    def worker_generate(self, runtime, text, params):
        voice = str(params.get("voice", DEFAULT_VOICE))
        speed = min(2.0, max(0.5, float(params.get("speed", 1.0))))
        return runtime.engine.speak(
            strip_tags(text.strip()), voice=voice, speed=speed, language=params.get("language", "English")
        )


ADAPTERS = {Kokoro.engine_id: Kokoro()}
