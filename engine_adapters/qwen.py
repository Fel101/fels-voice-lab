"""Qwen3-TTS: four checkpoints through faster-qwen3-tts in .venv-fqwen, each streaming its audio as it speaks. VoiceDesign
(1.7B) makes a voice from a description, the two Base checkpoints (1.7B and 0.6B) clone a voice recording, and CustomVoice
(1.7B) speaks in its preset speakers.

qwen_support.py holds their installer names and the files each checkpoint needs; qwen_worker.QwenWorker loads and speaks in
their worker."""

import os
import time

import qwen_support
from engine_adapters.base import Adapter

LANGUAGES = frozenset(
    {"English", "Chinese", "Japanese", "Korean", "German", "French", "Russian", "Portuguese", "Spanish", "Italian", "Auto"}
)
# What every Qwen request may hold, and what each mode adds.
COMMON_FIELDS = {
    "engine",
    "text",
    "mode",
    "language",
    "seed",
    "max_new_tokens",
    "save_voice_as",
    "temperature",
    "top_k",
    "top_p",
    "repetition_penalty",
}
MODE_FIELDS = {
    "design": {"voice_description"},
    "clone": {"reference_path", "reference_text", "xvec_only", "direction"},
    "custom_voice": {"speaker", "direction"},
}
SAMPLING = {"seed": 42, "max_new_tokens": 512, "temperature": 0.9, "top_k": 50, "top_p": 1.0, "repetition_penalty": 1.05}
SAMPLING_UI = {
    "seed": ("Seed", 0, 2147483647, 1),
    "max_new_tokens": ("Maximum new tokens", 8, 2048, 1),
    "temperature": ("Temperature", 0.05, 2.0, 0.05),
    "top_k": ("Top-k", 0, 200, 1),
    "top_p": ("Top-p", 0.05, 1.0, 0.01),
    "repetition_penalty": ("Repetition penalty", 0.5, 2.0, 0.05),
}
REFERENCE_POLICY = {
    "max_seconds": 60.0,
    "limit_basis": "voice-lab safety ceiling; the installed Qwen callable exposes no hard duration limit",
    "recommended_rate": 24_000,
    "requires_transcript_for_cast": True,
}
# The voice recording the language matrix and the Test clone, without its transcript (x-vector conditioning needs none).
CHECK_REFERENCE = ("reference", "af_heart.wav")


class Qwen(Adapter):
    """What the four checkpoints share."""

    mode = None  # the one generation mode the checkpoint has
    required_packages = ("faster-qwen3-tts",)
    required_files = qwen_support.QWEN_FILES
    gpu_build = "tools.install_qwen"
    sizes_install = True
    min_free_ram_gb = 10
    gpu_only = "Qwen3-TTS runs only on a graphics card in the Lab: its fast decoder records CUDA graphs."
    languages = LANGUAGES
    streams = True
    output_license = {
        "code_license": "MIT faster-qwen3-tts runtime; Apache-2.0 Qwen/Transformers compatibility components.",
        "weights_license": "Apache-2.0, Qwen3-TTS checkpoint; recording rights are separate.",
        "output_use": "Unknown; review checkpoint terms and reference rights before redistribution.",
        "status": "unknown",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json; complete upstream license.",
    }

    @property
    def guided_installer(self):
        return f"{qwen_support.QWEN_LAUNCHER} --model {qwen_support.QWEN_MODELS[self.engine_id]}"

    @property
    def installer(self):
        return (qwen_support.QWEN_INSTALLER, "--model", qwen_support.QWEN_MODELS[self.engine_id])

    @property
    def generation_mode(self):
        return self.mode

    # The service's half

    def check_mode(self, req):
        if req.get("mode", self.mode) != self.mode:
            raise ValueError(
                f"Checkpoint {self.engine_id!r} supports mode {self.mode!r}; it does not support {req.get('mode')!r}."
            )

    def check_speak(self, req, pool):
        self.check_mode(req)
        mode = self.mode
        unsupported = set(req) - COMMON_FIELDS - MODE_FIELDS[mode]
        if unsupported:
            raise ValueError(f"Unsupported Qwen {mode} parameter(s): {', '.join(sorted(unsupported))}.")
        self.check_mode_request(req)
        if req.get("save_voice_as") and mode != "design":
            raise ValueError("Only a Qwen VoiceDesign take can be saved as a new reusable voice reference.")

    def check_mode_request(self, req):
        """What this checkpoint's mode needs of a request."""

    def speak_params(self, req, pool, worker, checked):
        return self.request_params(req, pool)

    def request_params(self, req, pool):
        """What generate and generate_stream get besides the text."""
        return {
            "mode": self.mode,
            "language": req.get("language", "English"),
            **{name: req.get(name, default) for name, default in SAMPLING.items()},
        }

    def reference_call(self, reference, reference_text, xvec_only, exaggeration):
        return {"path": str(reference), "reference_text": reference_text, "xvec_only": xvec_only}

    def sampling_request(self, settings):
        return {name: settings.get(name, default) for name, default in SAMPLING.items()}

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_qwen as iq

        files = files_bytes(iq.base.load_manifest(root / iq.MODELS[self.engine_id]["manifest"]))
        download, disk = iq.packages_mb(card)
        return download * 10**6 + files, disk * 10**6 + files

    # The worker's half: qwen_worker.QwenWorker in .venv-fqwen

    def worker_load(self, runtime, params):
        from qwen_worker import QwenWorker

        if runtime.backend is None:
            runtime.backend = QwenWorker(runtime.engine_id, runtime.device)
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
            "supported_speakers",
            "supported_languages",
            "dtype",
            "attention",
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
        # output_file belongs to the broker/worker audio handoff protocol; it is not a user generation kwarg for the strict
        # Qwen adapter.
        return runtime.backend.generate({key: value for key, value in params.items() if key != "output_file"})

    def worker_generate_stream(self, runtime, params, emit_audio):
        return runtime.backend.generate_stream(params, emit_audio)

    def worker_unload(self, runtime):
        return {"qwen": runtime.backend.unload()} if runtime.backend is not None else {}


class QwenVoiceDesign(Qwen):
    engine_id = "qwen3-tts-1.7b-voice-design"
    mode = "design"
    saves_designs = True  # a design take, or stream, may be kept as a voice recording (save_voice_as)

    def check_mode_request(self, req):
        if not str(req.get("voice_description", "")).strip():
            raise ValueError("Qwen VoiceDesign requires a non-empty voice_description.")

    def request_params(self, req, pool):
        return {**super().request_params(req, pool), "voice_description": req.get("voice_description", "")}

    def smoke_request(self, base, loaded=None, root=None):
        from engine_checks import CHECK_VOICE

        return {**base, "mode": "design", "voice_description": CHECK_VOICE}

    def matrix_request(self, request, clip, loaded=None, root=None):
        from engine_checks import CHECK_VOICE

        return {**request, "mode": "design", "voice_description": CHECK_VOICE, "seed": clip["seed"], "language": clip["label"]}


class QwenBase(Qwen):
    """The Base checkpoints, which clone a voice recording."""

    mode = "clone"
    reference_voices = True
    reference_policy = REFERENCE_POLICY
    takes_transcript = True  # a voice of it keeps the recording's transcript, which each take sends
    takes_direction = True  # a take may carry an acting direction
    clone_check = True  # its Test clones the bundled clip, so the evaluation can compare the voices
    performance_defaults = SAMPLING
    control_ui = SAMPLING_UI

    def check_mode_request(self, req):
        if not req.get("reference_path"):
            raise ValueError("Qwen reference cloning requires reference_path.")
        if not isinstance(req.get("xvec_only", False), bool):
            raise ValueError("xvec_only must be true or false.")
        if not req.get("xvec_only", False) and not str(req.get("reference_text", "")).strip():
            raise ValueError("Qwen reference cloning requires reference_text unless xvec_only is enabled.")

    def request_params(self, req, pool):
        reference = pool._resolve_reference_path(self.engine_id, req["reference_path"])
        return {
            **super().request_params(req, pool),
            "reference_path": str(reference),
            "reference_text": req.get("reference_text", ""),
            "xvec_only": req.get("xvec_only", False),
            "direction": req.get("direction", ""),
        }

    def performance_request(self, identity, settings, applied, direction, language):
        if not isinstance(language, str) or not language.strip():
            raise ValueError("Choose a language for Qwen Base performance.")
        return {
            "mode": "clone",
            "language": language,
            "direction": direction,
            **self.sampling_request(settings),
            "xvec_only": False,
        }

    def smoke_request(self, base, loaded=None, root=None):
        from pathlib import Path

        from common import LAB

        # x-vector conditioning needs no transcript, and the bundled reference has none.
        return {**base, "reference_path": str(Path(root or LAB).joinpath(*CHECK_REFERENCE)), "xvec_only": True}

    def matrix_request(self, request, clip, loaded=None, root=None):
        request = self.smoke_request(request, loaded, root=root)
        return {**request, "language": clip["label"], "seed": clip["seed"]}


class QwenBase17(QwenBase):
    engine_id = "qwen3-tts-1.7b-base"


class QwenBase06(QwenBase):
    engine_id = "qwen3-tts-0.6b-base"


class QwenCustomVoice(Qwen):
    engine_id = "qwen3-tts-1.7b-custom-voice"
    mode = "custom_voice"
    preset_voices = True
    takes_direction = True
    performance_defaults = SAMPLING
    control_ui = SAMPLING_UI

    def check_mode_request(self, req):
        if not str(req.get("speaker", "")).strip():
            raise ValueError("Qwen CustomVoice requires a speaker returned by the loaded checkpoint.")

    def request_params(self, req, pool):
        return {**super().request_params(req, pool), "speaker": req.get("speaker", ""), "direction": req.get("direction", "")}

    def performance_request(self, identity, settings, applied, direction, language):
        options = identity["engine_options"]
        return {
            "mode": "custom_voice",
            "speaker": options["speaker"],
            "language": language,
            "direction": direction,
            **self.sampling_request(settings),
        }

    def identity_options(self, capabilities, req, controls, pool):
        entry = pool.api_entries[self.engine_id]
        loaded_info = getattr(pool, "loaded_model_info", lambda _key: None)(self.engine_id)
        if not loaded_info:
            raise ValueError("Load the CustomVoice checkpoint first so its local speakers and languages can be verified.")
        supported_speakers = loaded_info.get("supported_speakers") or []
        supported_languages = loaded_info.get("supported_languages") or []
        speaker = req.get("speaker")
        language = req.get("language")
        if not isinstance(speaker, str) or speaker not in supported_speakers:
            raise ValueError("Choose a speaker reported by the loaded CustomVoice checkpoint.")
        language_match = next(
            (item for item in supported_languages if isinstance(language, str) and item.casefold() == language.casefold()),
            None,
        )
        if language_match is None:
            raise ValueError("Choose a language reported by the loaded CustomVoice checkpoint.")
        default_direction = req.get("default_direction", "")
        if not isinstance(default_direction, str) or len(default_direction) > 1000:
            raise ValueError("Preset delivery instruction must be text of 1000 characters or fewer.")
        checkpoint = loaded_info.get("checkpoint") or entry["checkpoint"]
        checkpoint_revision = loaded_info.get("checkpoint_revision") or entry["local_revision"]
        if checkpoint != entry["checkpoint"] or checkpoint_revision != entry["local_revision"]:
            raise ValueError("The loaded CustomVoice model does not match the catalog's pinned checkpoint revision.")
        return {
            "checkpoint": checkpoint,
            "checkpoint_revision": checkpoint_revision,
            "speaker": speaker,
            "language": language_match,
            "supported_speakers": list(supported_speakers),
            "supported_languages": list(supported_languages),
            "default_direction": default_direction.strip(),
            "settings_schema": controls["controls"],
            "capability_snapshot": {
                "voice_direction": controls["direction"],
                "sampling_controls": capabilities.get("sampling_controls", {}),
                "acting_controls": capabilities.get("acting_controls", []),
            },
        }

    def smoke_request(self, base, loaded=None, root=None):
        speakers = (loaded or {}).get("supported_speakers") or []
        if not speakers:
            raise ValueError("The CustomVoice checkpoint reported no speakers to check.")
        return {**base, "speaker": speakers[0]}

    def matrix_request(self, request, clip, loaded=None, root=None):
        speakers = (loaded or {}).get("supported_speakers") or []
        if not speakers:
            raise ValueError("The CustomVoice checkpoint reported no speakers.")
        return {**request, "speaker": speakers[0], "language": clip["label"], "seed": clip["seed"]}


ADAPTERS = {adapter.engine_id: adapter for adapter in (QwenVoiceDesign(), QwenBase17(), QwenBase06(), QwenCustomVoice())}
