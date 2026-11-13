"""VoxCPM2: designs a voice from a description, clones a voice recording (with an acting direction), or continues one from its
transcript, from its pinned source (out/voxcpm2/source) with the packages of .venv-voxcpm.

voxcpm_worker.VoxCPMWorker loads and speaks in its worker; engine_worker.py puts its source on the worker's import path before
anything else is imported."""

import os
import time

from engine_adapters.base import Adapter

MODES = {"design", "clone", "continuation"}
COMMON_FIELDS = {"engine", "text", "mode", "seed", "cfg_value", "inference_timesteps", "save_voice_as"}
MODE_FIELDS = {
    "design": {"voice_description"},
    "clone": {"reference_path", "direction"},
    "continuation": {"reference_path", "prompt_path", "prompt_text"},
}


class VoxCPM2(Adapter):
    engine_id = "voxcpm2"
    guided_installer = "setup-voxcpm.bat"
    installer = "tools/install_voxcpm.py"
    gpu_build = "tools.install_voxcpm"
    required_packages = ("torch", "transformers", "librosa")
    required_files = ("model.safetensors", "audiovae.pth", "config.json", "tokenizer.json")
    # It reads its pinned snapshot from the top of hf-cache; the other Hugging Face engines use hf-cache/hub.
    top_level_cache = True
    # Its pinned source files, which tools/install_voxcpm.py fetches and its worker adds to its import path.
    extra_directories = ("out/voxcpm2/source/src",)
    # A .venv-voxcpm made by hand without PyTorch reads .venv-fqwen's packages, read-only (engine_worker.py adds them to its
    # import path), as the Lab first ran VoxCPM2 on an AMD card.
    borrowed_packages = ".venv-fqwen/Scripts/python.exe"
    sizes_install = True
    min_free_ram_gb = 10
    gpu_only = "VoxCPM2 runs only on a graphics card with at least 7 GB of memory."
    reference_voices = True
    reference_policy = {
        "max_seconds": 60.0,
        "limit_basis": "voice-lab safety ceiling for reference conditioning",
        "recommended_rate": 48_000,
        "requires_transcript_for_cast": False,
    }
    takes_direction = True
    saves_designs = True
    # Its model card lists 30 languages; the scene picker allows only English.
    upstream_languages = (
        "Arabic",
        "Burmese",
        "Chinese",
        "Danish",
        "Dutch",
        "English",
        "Finnish",
        "French",
        "German",
        "Greek",
        "Hebrew",
        "Hindi",
        "Indonesian",
        "Italian",
        "Japanese",
        "Khmer",
        "Korean",
        "Lao",
        "Malay",
        "Norwegian",
        "Polish",
        "Portuguese",
        "Russian",
        "Spanish",
        "Swahili",
        "Swedish",
        "Tagalog",
        "Thai",
        "Turkish",
        "Vietnamese",
    )
    performance_defaults = {"seed": 42, "cfg_value": 2.0, "inference_timesteps": 10}
    control_ui = {
        "seed": ("Seed", 0, 2147483647, 1),
        "cfg_value": ("CFG value", 0.1, 10.0, 0.1),
        "inference_timesteps": ("Inference steps", 1, 100, 1),
    }
    output_license = {
        "code_license": "Apache-2.0 per the pinned upstream repository.",
        "weights_license": "Apache-2.0 per the pinned model card.",
        "output_use": "Unknown; review reference rights and intended distribution.",
        "status": "unknown",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json; complete upstream license.",
    }

    # The service's half

    def check_speak(self, req, pool):
        mode = req.get("mode", "design")
        if mode not in MODES:
            raise ValueError("VoxCPM2 mode must be design, clone, or continuation.")
        unsupported = set(req) - COMMON_FIELDS - MODE_FIELDS[mode]
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

    def speak_params(self, req, pool, worker, checked):
        mode = req.get("mode", "design")
        params = {
            "mode": mode,
            "seed": req.get("seed", 42),
            "cfg_value": req.get("cfg_value", 2.0),
            "inference_timesteps": req.get("inference_timesteps", 10),
        }
        if mode == "design":
            params["voice_description"] = req.get("voice_description", "")
        elif mode == "clone":
            reference = pool._resolve_reference_path(self.engine_id, req["reference_path"])
            params["reference_path"] = str(reference)
            params["direction"] = req.get("direction", "")
            worker.call("prepare_reference", path=str(reference))
        else:
            reference = pool._resolve_reference_path(self.engine_id, req["reference_path"])
            prompt = pool._resolve_reference_path(self.engine_id, req["prompt_path"])
            params.update(
                {"reference_path": str(reference), "prompt_path": str(prompt), "prompt_text": req.get("prompt_text", "")}
            )
            worker.call("prepare_reference", path=str(reference))
        return params

    def reference_call(self, reference, reference_text, xvec_only, exaggeration):
        return {"path": str(reference)}

    def performance_request(self, identity, settings, applied, direction, language):
        return {
            "mode": "clone",
            "direction": direction,
            "seed": settings.get("seed", 42),
            "cfg_value": settings.get("cfg_value", 2.0),
            "inference_timesteps": settings.get("inference_timesteps", 10),
        }

    def mode_for(self, backend_request):
        return backend_request["mode"]

    def smoke_request(self, base, loaded=None, root=None):
        from engine_checks import CHECK_VOICE

        return {**base, "mode": "design", "voice_description": CHECK_VOICE}

    def matrix_request(self, request, clip, loaded=None, root=None):
        # It takes the language from the text itself.
        return {**self.smoke_request(request, loaded, root=root), "seed": clip["seed"]}

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_voxcpm as iv

        files = files_bytes(iv.base.load_manifest(root / iv.MANIFEST)) + files_bytes(iv.load_source(root / iv.SOURCE_MANIFEST))
        download, disk = iv.packages_mb(card)
        return download * 10**6 + files, disk * 10**6 + files

    # The worker's half: voxcpm_worker.VoxCPMWorker in .venv-voxcpm

    def worker_load(self, runtime, params):
        from voxcpm_worker import VoxCPMWorker

        if runtime.backend is None:
            runtime.backend = VoxCPMWorker(runtime.device)
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
            "dtype",
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

    def worker_prepare_reference(self, runtime, params):
        return runtime.backend.prepare_reference(params)

    def worker_generate(self, runtime, text, params):
        return runtime.backend.generate({key: value for key, value in params.items() if key != "output_file"})

    def worker_unload(self, runtime):
        return {"voxcpm2": runtime.backend.unload()} if runtime.backend is not None else {}


ADAPTERS = {VoxCPM2.engine_id: VoxCPM2()}
