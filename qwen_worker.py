"""Isolated faster-Qwen3-TTS adapter for the installed voice-lab checkpoints."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import time
from collections import OrderedDict

from common import LAB, require_ram
from ram_needs import MIN_FREE_RAM_GB
from reference_paths import resolve_reference_path

REFERENCE_SUFFIXES = {".wav", ".flac", ".ogg"}
MAX_REFERENCE_CACHE_ENTRIES = 8
LANGUAGE_LABELS = {
    "auto": "Auto", "chinese": "Chinese", "english": "English", "japanese": "Japanese",
    "korean": "Korean", "german": "German", "french": "French", "russian": "Russian",
    "portuguese": "Portuguese", "spanish": "Spanish", "italian": "Italian",
}

ENGINES = {
    "qwen3-tts-1.7b-voice-design": {
        "repo_id": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "revision": "5ecdb67327fd37bb2e042aab12ff7391903235d3",
        "mode": "design",
    },
    "qwen3-tts-1.7b-base": {
        "repo_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "revision": "fd4b254389122332181a7c3db7f27e918eec64e3",
        "mode": "clone",
    },
    "qwen3-tts-0.6b-base": {
        "repo_id": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        "revision": "5d83992436eae1d760afd27aff78a71d676296fc",
        "mode": "clone",
    },
    "qwen3-tts-1.7b-custom-voice": {
        "repo_id": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "revision": "0c0e3051f131929182e2c023b9537f8b1c68adfe",
        "mode": "custom_voice",
    },
}


def _number(params, name, default, low, high):
    value = params.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number between {low} and {high}.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number between {low} and {high}.") from None
    if not math.isfinite(value) or value < low or value > high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


def _integer(params, name, default, low, high):
    value = params.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer between {low} and {high}.")
    try:
        converted = int(value)
        if str(value).strip() not in (str(converted), f"{converted}.0"):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer between {low} and {high}.") from None
    if converted < low or converted > high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return converted


def _boolean(params, name, default=False):
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false.")
    return value


class QwenWorker:
    """One fixed checkpoint per worker; models and prompt caches never cross workers."""

    def __init__(self, engine_id: str, device: str):
        if engine_id not in ENGINES:
            raise ValueError(f"Unknown faster-Qwen engine {engine_id!r}.")
        self.engine_id = engine_id
        self.spec = ENGINES[engine_id]
        self.device = device
        self.model = None
        self.sample_rate = None
        self.loaded_at = None
        self.runtime_identity = None
        self.supported_speakers = []
        self.supported_languages = []
        self.reference_cache = OrderedDict()
        self.reference_content = {}

    def load(self):
        if self.model is not None:
            return self._load_info(already_loaded=True)
        if self.device != "cuda":
            raise RuntimeError("Qwen3-TTS runs only on a graphics card in the Lab, because its fast decoder records CUDA "
                               "graphs, and this worker has no graphics card it can use.")
        require_ram(MIN_FREE_RAM_GB[self.engine_id], self.engine_id)

        # Import the established compatibility shim before importing/loading the
        # faster implementation; it fixes the Transformers 5.x codec RoPE config.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import common  # noqa: F401 - enforces HIP selection and MIOpen workaround first
        import fqwen_compat  # noqa: F401
        import torch
        import transformers
        from faster_qwen3_tts import FasterQwen3TTS
        from huggingface_hub import snapshot_download

        started = time.perf_counter()
        snapshot = snapshot_download(
            repo_id=self.spec["repo_id"],
            revision=self.spec["revision"],
            local_files_only=True,
        )
        model = FasterQwen3TTS.from_pretrained(
            str(snapshot), device=self.device, dtype=torch.bfloat16,
            attn_implementation="sdpa", local_files_only=True,
        )
        # Preserve the existing faster-Qwen benchmark's graph warm-up strategy.
        model.warmup(prefill_len=100)
        self.model = model
        self.sample_rate = 24000
        self.loaded_at = time.time()
        self.supported_speakers = self._query_model_list("get_supported_speakers")
        self.supported_languages = self._query_model_list("get_supported_languages")
        self.runtime_identity = self._runtime_identity(torch, transformers)
        return {
            **self._load_info(already_loaded=False),
            "load_seconds": round(time.perf_counter() - started, 3),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "dtype": "bfloat16",
            "attention": "sdpa",
            "device_name": torch.cuda.get_device_name() if torch.cuda.is_available() else None,
        }

    def _query_model_list(self, name):
        query = getattr(self.model, name, None)
        if query is None:
            query = getattr(getattr(self.model, "model", None), name, None)
        value = query() if callable(query) else None
        if value is None:
            return []
        if name == "get_supported_languages":
            return [LANGUAGE_LABELS.get(str(item).casefold(), str(item)) for item in value]
        if name == "get_supported_speakers":
            return ["_".join(part[:1].upper() + part[1:] for part in str(item).split("_")) for item in value]
        return [str(item) for item in value]

    def _load_info(self, *, already_loaded):
        return {
            "already_loaded": already_loaded,
            "engine_id": self.engine_id,
            "model_loaded": self.model is not None,
            "device": self.device,
            "sample_rate": self.sample_rate,
            "backend": "faster-qwen3-tts",
            "backend_version": self._package_version("faster-qwen3-tts"),
            "checkpoint": self.spec["repo_id"],
            "checkpoint_revision": self.spec["revision"],
            "mode": self.spec["mode"],
            "runtime_identity": self.runtime_identity,
            "supported_speakers": self.supported_speakers,
            "supported_languages": self.supported_languages,
        }

    @staticmethod
    def _package_version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    def _runtime_identity(self, torch, transformers):
        source_hash = hashlib.sha256()
        for path in (LAB / "qwen_worker.py", LAB / "fqwen_compat.py"):
            source_hash.update(path.name.encode("utf-8"))
            source_hash.update(path.read_bytes())
        identity = {
            "engine_id": self.engine_id,
            "checkpoint": self.spec["repo_id"],
            "checkpoint_revision": self.spec["revision"],
            "backend": self._package_version("faster-qwen3-tts"),
            "qwen_tts_hf": self._package_version("qwen-tts-hf"),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "device": self.device,
            "dtype": "bfloat16",
            "attention": "sdpa",
            "source_sha256": source_hash.hexdigest(),
        }
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _reference_path(path):
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise ValueError("Qwen clone requires a local reference path.")
        resolved = resolve_reference_path("qwen3-tts-1.7b-base", path)
        if resolved.suffix.lower() not in REFERENCE_SUFFIXES:
            raise ValueError("Qwen reference audio must be WAV, FLAC, or OGG.")
        return resolved

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def prepare_reference(self, params):
        if self.model is None:
            raise RuntimeError("Load the Qwen model before preparing a reference.")
        path = self._reference_path(params.get("path"))
        transcript = params.get("reference_text", "")
        xvec_only = _boolean(params, "xvec_only", False)
        if not isinstance(transcript, str):
            raise ValueError("reference_text must be text.")
        transcript = transcript.strip()
        if not xvec_only and not transcript:
            raise ValueError("Qwen reference cloning requires the reference transcript unless speaker-embedding-only mode is selected.")
        if len(transcript) > 1200:
            raise ValueError("Reference transcript must be 1200 characters or fewer.")

        content_hash = self._sha256(path)
        library_key = (str(path), transcript, xvec_only, True)
        cache_key = hashlib.sha256(json.dumps({
            "reference_sha256": content_hash,
            "transcript": transcript,
            "xvec_only": xvec_only,
            "append_silence": True,
            "runtime_identity": self.runtime_identity,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        cached = self.reference_cache.get(cache_key)
        if cached is not None:
            self.reference_cache.move_to_end(cache_key)
            return {"prepared": True, "engine_id": self.engine_id, "path": str(path),
                    "reference_sha256": content_hash, "cache_hit": True,
                    "runtime_identity": self.runtime_identity}

        old_content = self.reference_content.get(library_key)
        prompt_cache = getattr(self.model, "_voice_prompt_cache", None)
        if old_content is not None and old_content != content_hash and prompt_cache is not None:
            prompt_cache.pop(library_key, None)
            for key in [key for key, value in self.reference_cache.items() if value[0] == library_key]:
                self.reference_cache.pop(key, None)
        if prompt_cache is not None and library_key not in prompt_cache:
            resolver = getattr(self.model, "_resolve_voice_clone_prompt_from_reference", None)
            if not callable(resolver):
                raise RuntimeError("The installed faster-Qwen runtime has no reference-prompt preparation method.")
            # The installed 0.4.0 resolver only uses input_ids for batch sizing in
            # speaker-embedding-only mode. A one-item placeholder prepares one ref.
            resolver([None], str(path), transcript, xvec_only, True)

        if prompt_cache is None:
            raise RuntimeError("The installed faster-Qwen runtime does not expose its reference prompt cache.")
        if library_key not in prompt_cache:
            raise RuntimeError("Qwen reference prompt preparation returned without a cached prompt.")
        self.reference_content[library_key] = content_hash
        self.reference_cache[cache_key] = (library_key, content_hash)
        self.reference_cache.move_to_end(cache_key)
        while len(self.reference_cache) > MAX_REFERENCE_CACHE_ENTRIES:
            _, (evicted_key, _) = self.reference_cache.popitem(last=False)
            prompt_cache.pop(evicted_key, None)
            self.reference_content.pop(evicted_key, None)
        return {"prepared": True, "engine_id": self.engine_id, "path": str(path),
                "reference_sha256": content_hash, "cache_hit": False,
                "runtime_identity": self.runtime_identity}

    def generate(self, params):
        if self.model is None:
            raise RuntimeError("Load the Qwen model before generation.")
        mode = params.get("mode", self.spec["mode"])
        if mode != self.spec["mode"]:
            raise ValueError(f"Checkpoint {self.engine_id!r} supports mode {self.spec['mode']!r}, not {mode!r}.")

        common_fields = {"text", "mode", "language", "seed", "max_new_tokens", "temperature",
                         "top_k", "top_p", "repetition_penalty"}
        mode_fields = {
            "design": {"voice_description"},
            "clone": {"reference_path", "reference_text", "xvec_only", "direction"},
            "custom_voice": {"speaker", "direction"},
        }[mode]
        extra = set(params) - common_fields - mode_fields
        if extra:
            raise ValueError(f"Unsupported Qwen {mode} parameter(s): {', '.join(sorted(extra))}.")

        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string.")
        text = text.strip()
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters.")
        language = params.get("language", "English")
        if not isinstance(language, str) or not language.strip():
            raise ValueError("language must be a non-empty language name.")
        language = language.strip()
        if self.supported_languages:
            match = next((value for value in self.supported_languages if value.casefold() == language.casefold()), None)
            if match is None:
                raise ValueError(f"Unsupported language {language!r}; this checkpoint reports: {', '.join(self.supported_languages)}.")
            language = match

        seed = _integer(params, "seed", 42, 0, 2_147_483_647)
        max_new_tokens = _integer(params, "max_new_tokens", 512, 8, 2048)
        sampling = {
            "max_new_tokens": max_new_tokens,
            "temperature": _number(params, "temperature", 0.9, 0.05, 2.0),
            "top_k": _integer(params, "top_k", 50, 0, 200),
            "top_p": _number(params, "top_p", 1.0, 0.05, 1.0),
            "do_sample": True,
            "repetition_penalty": _number(params, "repetition_penalty", 1.05, 0.5, 2.0),
        }

        import numpy as np
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

        reference_meta = None
        voice_description = None
        direction = None
        speaker = None
        started = time.perf_counter()
        if mode == "design":
            voice_description = params.get("voice_description")
            if not isinstance(voice_description, str) or not voice_description.strip():
                raise ValueError("Qwen VoiceDesign requires a non-empty voice_description.")
            voice_description = voice_description.strip()
            if len(voice_description) > 1000:
                raise ValueError("Voice descriptions must be 1000 characters or fewer.")
            wavs, sample_rate = self.model.generate_voice_design(
                text=text, instruct=voice_description, language=language, **sampling,
            )
        elif mode == "clone":
            path = self._reference_path(params.get("reference_path"))
            transcript = params.get("reference_text", "")
            xvec_only = _boolean(params, "xvec_only", False)
            reference_meta = self.prepare_reference({
                "path": str(path), "reference_text": transcript, "xvec_only": xvec_only,
            })
            direction = params.get("direction", "")
            if not isinstance(direction, str) or len(direction) > 1000:
                raise ValueError("Experimental Base direction must be text of 1000 characters or fewer.")
            wavs, sample_rate = self.model.generate_voice_clone(
                text=text, language=language, ref_audio=str(path), ref_text=transcript.strip(),
                xvec_only=xvec_only, instruct=direction.strip() or None, **sampling,
            )
        else:
            speaker = params.get("speaker")
            if not isinstance(speaker, str) or not speaker.strip():
                raise ValueError("Qwen CustomVoice requires a speaker returned by the loaded model.")
            speaker = speaker.strip()
            if self.supported_speakers:
                match = next((value for value in self.supported_speakers if value.casefold() == speaker.casefold()), None)
                if match is None:
                    raise ValueError(f"Unsupported CustomVoice speaker {speaker!r}; choose one of: {', '.join(self.supported_speakers)}.")
                speaker = match
            direction = params.get("direction", "")
            if not isinstance(direction, str) or len(direction) > 1000:
                raise ValueError("CustomVoice direction must be text of 1000 characters or fewer.")
            wavs, sample_rate = self.model.generate_custom_voice(
                text=text, speaker=speaker, language=language,
                instruct=direction.strip() or None, **sampling,
            )

        if not wavs:
            raise RuntimeError("Qwen returned no audio samples.")
        audio = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        if audio.size == 0 or not np.isfinite(audio).all():
            raise RuntimeError("Qwen returned empty or non-finite audio.")
        self.sample_rate = int(sample_rate)
        info = {
            "total_s": time.perf_counter() - started,
            "first_chunk_s": None,
            "engine": self.engine_id,
            "mode": mode,
            "backend": "faster-qwen3-tts",
            "backend_version": self._package_version("faster-qwen3-tts"),
            "checkpoint": self.spec["repo_id"],
            "checkpoint_revision": self.spec["revision"],
            "runtime_identity": self.runtime_identity,
            "device": self.device,
            "dtype": "bfloat16",
            "attention": "sdpa",
            "language": language,
            "sampling": {"seed": seed, **sampling},
            "voice_description": voice_description,
            "direction": direction.strip() if direction else None,
            "direction_status": "experimental_base_instruction" if mode == "clone" and direction else None,
            "speaker": speaker,
            "supported_speakers": self.supported_speakers if mode == "custom_voice" else None,
            "reference_sha256": reference_meta["reference_sha256"] if reference_meta else None,
            "reference_cache_hit": reference_meta["cache_hit"] if reference_meta else None,
            "xvec_only": bool(params.get("xvec_only", False)) if mode == "clone" else None,
        }
        return audio, sample_rate, info

    def generate_stream(self, params, on_chunk):
        """Incrementally decode faster-Qwen's native streaming generator to mono float chunks."""
        if self.model is None:
            raise RuntimeError("Load the Qwen model before generation.")
        mode = params.get("mode", self.spec["mode"])
        if mode != self.spec["mode"]:
            raise ValueError(f"Checkpoint {self.engine_id!r} supports mode {self.spec['mode']!r}, not {mode!r}.")

        common_fields = {"text", "mode", "language", "seed", "max_new_tokens", "temperature",
                         "top_k", "top_p", "repetition_penalty"}
        mode_fields = {
            "design": {"voice_description"},
            "clone": {"reference_path", "reference_text", "xvec_only", "direction"},
            "custom_voice": {"speaker", "direction"},
        }[mode]
        extra = set(params) - common_fields - mode_fields
        if extra:
            raise ValueError(f"Unsupported Qwen {mode} parameter(s): {', '.join(sorted(extra))}.")
        if not callable(on_chunk):
            raise TypeError("on_chunk must be callable.")

        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string.")
        text = text.strip()
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters.")
        language = params.get("language", "English")
        if not isinstance(language, str) or not language.strip():
            raise ValueError("language must be a non-empty language name.")
        language = language.strip()
        if self.supported_languages:
            match = next((value for value in self.supported_languages if value.casefold() == language.casefold()), None)
            if match is None:
                raise ValueError(f"Unsupported language {language!r}; this checkpoint reports: {', '.join(self.supported_languages)}.")
            language = match

        seed = _integer(params, "seed", 42, 0, 2_147_483_647)
        max_new_tokens = _integer(params, "max_new_tokens", 512, 8, 2048)
        sampling = {
            "max_new_tokens": max_new_tokens,
            "temperature": _number(params, "temperature", 0.9, 0.05, 2.0),
            "top_k": _integer(params, "top_k", 50, 0, 200),
            "top_p": _number(params, "top_p", 1.0, 0.05, 1.0),
            "do_sample": True,
            "repetition_penalty": _number(params, "repetition_penalty", 1.05, 0.5, 2.0),
        }

        import numpy as np
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)

        reference_meta = None
        voice_description = None
        direction = None
        speaker = None
        chunk_size = 8
        started = time.perf_counter()
        if mode == "design":
            voice_description = params.get("voice_description")
            if not isinstance(voice_description, str) or not voice_description.strip():
                raise ValueError("Qwen VoiceDesign requires a non-empty voice_description.")
            voice_description = voice_description.strip()
            if len(voice_description) > 1000:
                raise ValueError("Voice descriptions must be 1000 characters or fewer.")
            method = getattr(self.model, "generate_voice_design_streaming", None)
            if not callable(method):
                raise RuntimeError("Installed faster-Qwen runtime has no VoiceDesign streaming generator.")
            generator = method(text=text, instruct=voice_description, language=language,
                               chunk_size=chunk_size, **sampling)
        elif mode == "clone":
            path = self._reference_path(params.get("reference_path"))
            transcript = params.get("reference_text", "")
            xvec_only = _boolean(params, "xvec_only", False)
            reference_meta = self.prepare_reference({
                "path": str(path), "reference_text": transcript, "xvec_only": xvec_only,
            })
            direction = params.get("direction", "")
            if not isinstance(direction, str) or len(direction) > 1000:
                raise ValueError("Experimental Base direction must be text of 1000 characters or fewer.")
            method = getattr(self.model, "generate_voice_clone_streaming", None)
            if not callable(method):
                raise RuntimeError("Installed faster-Qwen runtime has no VoiceClone streaming generator.")
            generator = method(text=text, language=language, ref_audio=str(path), ref_text=transcript.strip(),
                               xvec_only=xvec_only, instruct=direction.strip() or None,
                               chunk_size=chunk_size, **sampling)
        else:
            speaker = params.get("speaker")
            if not isinstance(speaker, str) or not speaker.strip():
                raise ValueError("Qwen CustomVoice requires a speaker returned by the loaded model.")
            speaker = speaker.strip()
            if self.supported_speakers:
                match = next((value for value in self.supported_speakers if value.casefold() == speaker.casefold()), None)
                if match is None:
                    raise ValueError(f"Unsupported CustomVoice speaker {speaker!r}; choose one of: {', '.join(self.supported_speakers)}.")
                speaker = match
            direction = params.get("direction", "")
            if not isinstance(direction, str) or len(direction) > 1000:
                raise ValueError("CustomVoice direction must be text of 1000 characters or fewer.")
            method = getattr(self.model, "generate_custom_voice_streaming", None)
            if not callable(method):
                raise RuntimeError("Installed faster-Qwen runtime has no CustomVoice streaming generator.")
            generator = method(text=text, speaker=speaker, language=language,
                               instruct=direction.strip() or None, chunk_size=chunk_size, **sampling)

        sample_rate = None
        sample_count = 0
        backend_chunk_count = 0
        generator_first_chunk_s = None
        for result in generator:
            try:
                wav, chunk_rate, _backend_timing = result
            except (TypeError, ValueError) as exc:
                raise RuntimeError("faster-Qwen streaming generator returned a malformed chunk.") from exc
            chunk_rate = int(chunk_rate)
            if not 8000 <= chunk_rate <= 192000:
                raise RuntimeError(f"faster-Qwen returned invalid sample rate {chunk_rate}.")
            if sample_rate is not None and chunk_rate != sample_rate:
                raise RuntimeError("faster-Qwen changed sample rate within one generation.")
            if hasattr(wav, "detach"):
                wav = wav.detach().cpu().numpy()
            audio = np.asarray(wav, dtype=np.float32).reshape(-1)
            if audio.size == 0 or not np.isfinite(audio).all():
                raise RuntimeError("faster-Qwen yielded empty or non-finite audio samples.")
            if generator_first_chunk_s is None:
                generator_first_chunk_s = time.perf_counter() - started
            sample_rate = chunk_rate
            sample_count += int(audio.size)
            backend_chunk_count += 1
            on_chunk(audio, chunk_rate, {
                "generator_first_chunk_s": generator_first_chunk_s,
                "backend_chunk_index": backend_chunk_count - 1,
            })

        if backend_chunk_count == 0 or sample_rate is None:
            raise RuntimeError("faster-Qwen streaming generator returned no audio chunks.")
        self.sample_rate = sample_rate
        return {
            "total_s": time.perf_counter() - started,
            "generator_first_chunk_s": generator_first_chunk_s,
            "backend_chunk_count": backend_chunk_count,
            "sample_count": sample_count,
            "engine": self.engine_id,
            "mode": mode,
            "backend": "faster-qwen3-tts",
            "backend_version": self._package_version("faster-qwen3-tts"),
            "checkpoint": self.spec["repo_id"],
            "checkpoint_revision": self.spec["revision"],
            "runtime_identity": self.runtime_identity,
            "device": self.device,
            "dtype": "bfloat16",
            "attention": "sdpa",
            "language": language,
            "sampling": {"seed": seed, **sampling},
            "voice_description": voice_description,
            "direction": direction.strip() if direction else None,
            "direction_status": "experimental_base_instruction" if mode == "clone" and direction else None,
            "speaker": speaker,
            "reference_sha256": reference_meta["reference_sha256"] if reference_meta else None,
            "reference_cache_hit": reference_meta["cache_hit"] if reference_meta else None,
            "xvec_only": bool(params.get("xvec_only", False)) if mode == "clone" else None,
            "streaming_kind": "native_generator",
        }

    def unload(self):
        entries = len(self.reference_cache)
        if self.model is not None:
            prompt_cache = getattr(self.model, "_voice_prompt_cache", None)
            if prompt_cache is not None:
                prompt_cache.clear()
        self.reference_cache.clear()
        self.reference_content.clear()
        self.supported_speakers = []
        self.supported_languages = []
        self.model = None
        self.sample_rate = None
        self.loaded_at = None
        self.runtime_identity = None
        return {"model_loaded": False, "reference_cache_entries_cleared": entries}
