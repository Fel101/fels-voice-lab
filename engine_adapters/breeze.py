"""Breeze TTS 2: designs a voice from a description, or clones a voice recording with or without an acting direction, from
its own files in breeze/models with the packages of .venv-breeze. It loads in one of two runtime profiles, eager or CUDA
graphs; asking for the other restarts its worker.

breeze_worker.BreezeWorker loads and speaks in its worker; engine_checks.BREEZE_FOLDER and BREEZE_FILES say where its files
are."""

import os
import time
from pathlib import Path

from engine_adapters.base import Adapter


class Breeze(Adapter):
    engine_id = "breeze-tts-2"
    guided_installer = "setup-breeze.bat"
    installer = "tools/install_breeze.py"
    gpu_build = "tools.install_breeze"
    required_packages = ("transformers", "accelerate")
    sizes_install = True
    min_free_ram_gb = 12
    gpu_only = "Breeze TTS 2 runs only on a graphics card in the Lab."
    profiles = True
    reference_voices = True
    reference_policy = {
        "max_seconds": 60.0,
        "limit_basis": "native Breeze MAX_REFERENCE_SECONDS",
        "recommended_rate": 24_000,
        "requires_transcript_for_cast": True,
    }
    takes_transcript = True
    takes_direction = True
    saves_designs = True
    acted_markup = "round"  # it acts (laugh), (sigh) and (cough) instead of saying them (word_timing.py)
    languages = frozenset({"English"})  # Only English has been verified on the local service path.
    # Its documentation says Chinese was checked locally (the language matrix).
    documented_languages = ("Chinese",)
    performance_defaults = {
        "profile": "eager",
        "seed": 42,
        "cfg_scale": 1.0,
        "max_new_tokens": 1500,
        "temperature": 0.9,
        "top_k": 50,
        "top_p": 1.0,
        "repetition_penalty": 1.1,
        "depth_temperature": 0.9,
        "depth_top_k": 50,
        "depth_top_p": 1.0,
    }
    control_ui = {
        "profile": ("Runtime profile", "eager", "cuda_graphs"),
        "seed": ("Seed", 0, 2147483647, 1),
        "cfg_scale": ("CFG scale", 0.0, 20.0, 0.1),
        "max_new_tokens": ("Maximum new tokens", 8, 2048, 1),
        "temperature": ("Temperature", 0.05, 2.0, 0.05),
        "top_k": ("Top-k", 0, 200, 1),
        "top_p": ("Top-p", 0.05, 1.0, 0.01),
        "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
        "depth_temperature": ("Depth temperature", 0.05, 2.0, 0.05),
        "depth_top_k": ("Depth top-k", 0, 200, 1),
        "depth_top_p": ("Depth top-p", 0.05, 1.0, 0.01),
    }
    output_license = {
        "code_license": "Apache-2.0; retained upstream notices in licenses/ and THIRD_PARTY_NOTICES.md.",
        "weights_license": "BreezeBlue Research and Non-Commercial License 1.1; covers derived checkpoints.",
        "output_use": "Restricted: self-hosted outputs subject to research/non-commercial terms; review the complete model "
        "license.",
        "status": "restricted",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json; complete upstream license.",
    }

    # The service's half

    def speak_params(self, req, pool, worker, checked):
        import worker_pool

        mode = str(req.get("mode", "design"))
        params = {
            "mode": mode,
            "profile": str(req.get("profile", "eager") or "eager"),
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
        }
        if mode in {"clone", "direction"}:
            reference_path = req.get("reference_path") or (worker_pool.LAB / "reference" / "af_heart.wav")
            reference = pool._resolve_reference_path(self.engine_id, reference_path)
            params["reference_path"] = str(reference)
            params["reference_text"] = req.get("reference_text", "")
            worker.call("prepare_reference", path=str(reference))
        return params

    def reference_call(self, reference, reference_text, xvec_only, exaggeration):
        return {"path": str(reference)}

    def performance_request(self, identity, settings, applied, direction, language):
        return {
            "mode": "direction" if direction.strip() else "clone",
            "profile": settings.get("profile", "eager"),
            "direction": direction,
            "seed": settings.get("seed", 42),
            "cfg_scale": settings.get("cfg_scale", 1.0),
            "max_new_tokens": settings.get("max_new_tokens", 1500),
            "temperature": settings.get("temperature", 0.9),
            "top_k": settings.get("top_k", 50),
            "top_p": settings.get("top_p", 1.0),
            "repetition_penalty": settings.get("repetition_penalty", 1.1),
            "depth_temperature": settings.get("depth_temperature", 0.9),
            "depth_top_k": settings.get("depth_top_k", 50),
            "depth_top_p": settings.get("depth_top_p", 1.0),
        }

    def mode_for(self, backend_request):
        return backend_request["mode"]

    def smoke_request(self, base, loaded=None, root=None):
        from engine_checks import CHECK_VOICE

        return {**base, "mode": "design", "voice_description": CHECK_VOICE}

    def matrix_request(self, request, clip, loaded=None, root=None):
        # It takes the language from the text itself.
        return {**self.smoke_request(request, loaded, root=root), "seed": clip["seed"]}

    def checkpoint_missing(self, root):
        from engine_checks import BREEZE_FILES, BREEZE_FOLDER

        return BREEZE_FOLDER, [
            f"{BREEZE_FOLDER}/{name}" for name in BREEZE_FILES if not (root / BREEZE_FOLDER / name).is_file()
        ]

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_breeze as ib

        files = files_bytes(ib.base.load_manifest(root / ib.MANIFEST))
        download, disk = ib.packages_mb(card)
        return download * 10**6 + files, disk * 10**6 + files

    # The worker's half: breeze_worker.BreezeWorker in .venv-breeze

    def worker_load(self, runtime, params):
        from breeze_worker import BreezeWorker

        if runtime.backend is None:
            runtime.backend = BreezeWorker(runtime.device)
        result = runtime.backend.load(params.get("profile", "eager"))
        runtime.engine = runtime.backend.bundle
        runtime.sample_rate = int(result["sample_rate"])
        runtime.profile = result["profile"]
        runtime.loaded_at = time.time()
        runtime.last_memory = runtime._memory()
        return {**result, "pid": os.getpid(), "parent_pid": os.getppid(), "memory": runtime.last_memory}

    def worker_prepare_reference(self, runtime, params):
        return runtime.backend.prepare_reference(str(Path(params.get("path", "")).expanduser().resolve()))

    def worker_generate(self, runtime, text, params):
        return runtime.backend.generate(params)

    def worker_unload(self, runtime):
        return {"breeze": runtime.backend.unload()} if runtime.backend is not None else {}


ADAPTERS = {Breeze.engine_id: Breeze()}
