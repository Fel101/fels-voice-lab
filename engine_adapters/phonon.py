"""Phonon 31M English: 37 preset voices, on the CPU, through the ptts runtime in .venv-phonon.

phonon_support.py reads what is installed (the voices, the pins) and checks requests; engines.Phonon is the model in its
worker. This module is what the rest of the Lab asks about Phonon."""

import os
import time

import phonon_support
from common import strip_tags
from engine_adapters.base import Adapter


class Phonon(Adapter):
    engine_id = phonon_support.PHONON_ENGINE_ID
    guided_installer = "setup-phonon.bat"
    installer = "tools/install_phonon.py"
    required_packages = ("ptts",)
    # Phonon peaked at about 0.5 GB on 1,200 characters.
    min_free_ram_gb = 2
    uses_torch = False
    cpu_only = "Phonon runs only on the CPU: its runtime has no graphics card code."
    direction_refusal = "Phonon has no text-direction control; its pace, seed and temperature are the settings."
    preset_voices = True
    languages = frozenset({"English"})
    performance_defaults = dict(phonon_support.DEFAULTS)
    control_ui = {
        "seed": ("Seed", *phonon_support.SEED_RANGE, 1),
        "temperature": ("Temperature", *phonon_support.TEMPERATURE_RANGE, 0.05),
        "duration_delta": ("Pace shift (negative is faster)", *phonon_support.DURATION_DELTA_RANGE, 0.1),
    }
    generation_mode = "fixed_voice"
    output_license = {
        "code_license": "MIT OR Apache-2.0 ptts runtime, separately installed.",
        "weights_license": "Apache-2.0 per the gradium/phonon-31m-en model card, which covers the weights, tokenizer and "
        "default voice of that repository. The 37 preset voices come from gradium/phonon-voices, which states no licence.",
        "output_use": "Unknown: nothing states what audio made with the catalog voices may be used for. Ask Gradium before "
        "redistributing it, and keep the model card's note that listeners should be told when speech is synthetic.",
        "status": "unknown",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json and catalog/phonon_snapshot.json; the model card.",
    }

    # The service's half

    def check_speak(self, req, pool):
        return phonon_support.check_request(req)

    def speak_params(self, req, pool, worker, checked):
        return checked

    def performance_request(self, identity, settings, applied, direction, language):
        return {"voice": identity["engine_options"]["speaker"], "language": language, **applied}

    def identity_options(self, capabilities, req, controls, pool):
        # Its voices are read from the installed catalog, so nothing has to be loaded first.
        return phonon_support.identity_options(capabilities, req.get("speaker"), req.get("language"), controls)

    def smoke_request(self, base, loaded=None, root=None):
        return {**base, "voice": phonon_support.default_voice(phonon_support.voice_names(root))}

    def matrix_request(self, request, clip, loaded=None, root=None):
        return {**request, "voice": phonon_support.default_voice(phonon_support.voice_names(root)), "seed": clip["seed"]}

    def info(self):
        return {"phonon_voices": phonon_support.discover_phonon_voices()}

    sizes_install = True

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_phonon as ip

        manifest = ip.load_manifest(root / ip.MANIFEST)
        files = sum(files_bytes(repo) for repo in manifest["repos"])
        return ip.PACKAGES_DOWNLOAD_MB * 10**6 + files, ip.PACKAGES_DISK_MB * 10**6 + files

    # The worker's half: engines.Phonon in .venv-phonon

    def worker_load(self, runtime, params):
        from engines import build_engine

        started = time.perf_counter()
        print(f"Loading {runtime.engine_id} on {runtime.device} (worker {os.getpid()}) ...", flush=True)
        model = build_engine(runtime.engine_id, runtime.device)
        runtime.engine = model
        runtime.sample_rate = int(model.sr)
        print("Warming up the model ...", flush=True)
        try:
            model.speak("Hello there.")
        except Exception as exc:
            print(f"Warm-up failed: {type(exc).__name__}: {exc}", flush=True)
        runtime.loaded_at = time.time()
        runtime.last_memory = runtime._memory()
        checkpoint, revision = model.pins["model"]
        runtime.model_info = {
            "backend": "ptts",
            "backend_version": model.version,
            "checkpoint": checkpoint,
            "checkpoint_revision": revision,
            "supported_speakers": list(model.voices),
            "supported_languages": ["English"],
        }
        return {
            "already_loaded": False,
            "engine_id": runtime.engine_id,
            "model_loaded": True,
            "device": model.device,
            "load_seconds": round(time.perf_counter() - started, 3),
            "sample_rate": runtime.sample_rate,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "memory": runtime.last_memory,
            **runtime.model_info,
        }

    def worker_prepare_reference(self, runtime, params):
        raise ValueError("Phonon uses its preset voices and does not accept reference audio.")

    def worker_generate(self, runtime, text, params):
        return runtime.engine.speak(
            strip_tags(text.strip()),
            voice=params.get("voice"),
            seed=params.get("seed", 42),
            temperature=params.get("temperature", 0.3),
            duration_delta=params.get("duration_delta", 0.0),
        )


ADAPTERS = {Phonon.engine_id: Phonon()}
