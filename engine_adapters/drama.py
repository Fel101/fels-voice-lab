"""Drama 3: Fish Audio's model (a Q4 derivative) through the speech.cpp runtime in out/drama3. It designs a voice from a
description or clones a voice recording with its exact transcript, and acts [whispers] and other cues written in the line,
on the CPU. Its own studio page (drama3_lab_studio.py) streams whole takes through EnginePool.stream_drama3.

drama3_runtime.py holds its settings, drama3_production.py its languages and scripts, and drama3_worker.Drama3Worker loads
and speaks in its worker. engine_checks._drama_missing reads its install receipt."""

import os
import time

from drama3_production import LANGUAGES
from engine_adapters.base import Adapter


class Drama3(Adapter):
    engine_id = "drama-3"
    guided_installer = "setup-drama3.bat"
    installer = "tools/install_drama3.py"
    sizes_install = True
    min_free_ram_gb = 26
    uses_torch = False  # speech.cpp chooses its own device
    cpu_only = "Drama 3 runs on the CPU in the Lab; docs/SETUP.md describes its experimental GPU setting for short designs."
    studio = True
    reference_voices = True
    reference_policy = {
        "max_seconds": 60.0,
        "recommended_rate": 44100,
        "requires_transcript_for_cast": True,
        "limit_basis": "Fel's Voice Lab reference ceiling; native voice files require exact transcripts",
    }
    takes_transcript = True
    takes_direction = True
    acted_markup = "square"  # [whispers] and other delivery cues, with <|speaker:N|> markers (word_timing.py)
    languages = frozenset({*LANGUAGES.values(), "Auto"})
    language_is_metadata = "the speech runtime does not receive the label"
    performance_defaults = {"seed": 42, "temperature": 1.0, "top_k": 30, "top_p": 0.9, "max_seconds": 47.6}
    control_ui = {
        "seed": ("Seed", 0, 2147483647, 1),
        "temperature": ("Temperature", 0.05, 2.0, 0.05),
        "top_k": ("Top-k", 1, 4097, 1),
        "top_p": ("Top-p", 0.05, 1.0, 0.01),
        "max_seconds": ("Maximum seconds per chunk", 0.5, 95.1, 0.1),
    }
    output_license = {
        "code_license": "MIT speech.cpp runtime, separately installed; runtime dependencies retain their own terms.",
        "weights_license": "Fish Audio Community License 1.0, including Q4 derivative.",
        "output_use": "Conditional commercial use, aggregate affiliate revenue below USD 500,000; hosted TTS/voice-cloning "
        "services need separate agreement. Built with Fish Audio. Review full model and reference terms.",
        "status": "restricted",
        "evidence": "https://huggingface.co/fishaudio/drama-3/blob/e0a9218aa48888270f06ef1ed9ea67622c0e17d2/LICENSE; "
        "derivative NOTICE.",
    }

    # The service's half

    def check_speak(self, req, pool):
        from drama3_runtime import controls

        allowed = {
            "engine",
            "text",
            "mode",
            "voice_description",
            "direction",
            "reference_path",
            "reference_text",
            "language",
            *controls(),
        }
        if set(req) - allowed:
            raise ValueError("Unsupported Drama 3 parameter(s): " + ", ".join(sorted(set(req) - allowed)))
        mode = req.get("mode", "design")
        if mode not in ("design", "clone"):
            raise ValueError("Drama 3 mode must be design or clone.")
        if mode == "clone":
            pool._resolve_reference_path(self.engine_id, req.get("reference_path"))
            if not isinstance(req.get("reference_text"), str) or not req["reference_text"].strip():
                raise ValueError("Drama 3 cloning requires the exact reference transcript.")
        controls({name: req[name] for name in controls() if name in req})

    def speak_params(self, req, pool, worker, checked):
        params = {name: value for name, value in req.items() if name not in ("engine", "text", "language")}
        if params.get("mode") == "clone":
            params["reference_path"] = str(pool._resolve_reference_path(self.engine_id, params.get("reference_path")))
        return params

    def check_prepare(self, reference_text):
        if not isinstance(reference_text, str) or not reference_text.strip():
            raise ValueError("Drama 3 reference preparation requires the exact transcript.")

    def reference_call(self, reference, reference_text, xvec_only, exaggeration):
        return {"path": str(reference), "reference_text": reference_text}

    def own_settings(self, settings):
        from drama3_runtime import controls

        return controls(settings)

    def performance_request(self, identity, settings, applied, direction, language):
        return dict(
            mode="clone",
            reference_text=identity["reference"]["transcript"],
            direction=direction,
            language=language,
            **applied,
        )

    def mode_for(self, backend_request):
        return backend_request["mode"]

    def smoke_request(self, base, loaded=None, root=None):
        from engine_checks import CHECK_VOICE

        return {**base, "mode": "design", "voice_description": CHECK_VOICE}

    def matrix_request(self, request, clip, loaded=None, root=None):
        return {**self.smoke_request(request, loaded, root=root), "seed": clip["seed"], "language": clip["label"]}

    def checkpoint_missing(self, root):
        from engine_checks import _drama_missing

        return "out/drama3", _drama_missing(root)

    def install_size(self, root, nvidia, card, files_bytes):
        from tools import install_drama3 as idr

        assets = sum(item["size"] for item in idr.ASSETS)
        # The runtime's archive is kept beside what it unpacks to, which the installer allows four times its size for.
        return idr.RUNTIME_SIZE + idr.MODEL_SIZE + assets, idr.RUNTIME_SIZE * 4 + idr.MODEL_SIZE + assets

    # The worker's half: drama3_worker.Drama3Worker, on speech.cpp

    def worker_load(self, runtime, params):
        from drama3_worker import Drama3Worker

        runtime.backend = Drama3Worker("cpu" if params.get("cpu_only") else runtime.device)
        result = runtime.backend.load()
        runtime.engine = runtime.backend
        runtime.sample_rate = 44100
        runtime.loaded_at = time.time()
        runtime.model_info = result
        return {**result, "pid": os.getpid(), "parent_pid": os.getppid()}

    def worker_prepare_reference(self, runtime, params):
        return runtime.backend.prepare_reference(params)

    def worker_generate(self, runtime, text, params):
        return runtime.backend.generate(params)

    def worker_generate_stream(self, runtime, params, emit_audio):
        import numpy as np

        def emit(pcm, rate):
            emit_audio(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0, rate)

        audio, rate, info = runtime.backend.generate(params, on_audio=emit)
        return {**info, "sample_count": len(audio), "sample_rate": rate, "pid": os.getpid()}

    def worker_unload(self, runtime):
        if runtime.backend is not None:
            runtime.backend.unload()
            runtime.backend = None
        return {}


ADAPTERS = {Drama3.engine_id: Drama3()}
