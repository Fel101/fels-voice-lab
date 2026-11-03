"""Service adapter for the installed Breeze TTS 2 runtime.

This module is imported only by the isolated .venv-breeze worker. It reuses
the project's existing BF16/SDPA wrapper and refuses to fetch missing files.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import OrderedDict
from pathlib import Path

from common import LAB, require_ram
from ram_needs import MIN_FREE_RAM_GB
from reference_paths import resolve_reference_path

ENGINE_ID = "breeze-tts-2"
CHECKPOINT_REVISION = "a6bfd9d0d4e8b3b61c3a5ed3ecf55468c8ab88e4"
REFERENCE_SUFFIXES = {".wav", ".flac", ".ogg"}
PROFILES = {"eager", "cuda_graphs"}
DEFAULT_DESCRIPTION = "A warm, clear, natural speaking voice."
MAX_REFERENCE_CACHE_ENTRIES = 8


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


class BreezeWorker:
    def __init__(self, device):
        self.device = device
        self.module = None
        self.bundle = None
        self.profile = None
        self.reference_cache = OrderedDict()
        self.runtime_identity = None

    def load(self, profile="eager"):
        profile = str(profile or "eager")
        if profile not in PROFILES:
            raise ValueError(f"Unknown Breeze decode profile {profile!r}; choose eager or cuda_graphs.")
        if self.bundle is not None:
            if self.profile != profile:
                raise ValueError(f"Breeze worker is loaded with profile {self.profile!r}; reload it for {profile!r}.")
            return {"already_loaded": True, "profile": self.profile, "sample_rate": 24000}

        require_ram(MIN_FREE_RAM_GB["breeze-tts-2"], "Breeze TTS 2")
        os.environ["BREEZE_DECODE"] = profile
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        started = time.perf_counter()
        import breeze_gen

        self.module = breeze_gen
        bundle = breeze_gen.load_model()
        if bundle.decode_mode != profile:
            raise RuntimeError(f"Requested Breeze profile {profile!r}, loaded {bundle.decode_mode!r}.")
        self.bundle = bundle
        self.profile = profile
        self.runtime_identity = self._runtime_identity()

        # A successful load includes a real short generation so missing or
        # incompatible local weights cannot be mistaken for a working engine.
        breeze_gen.design_speech("Ready when you are.", DEFAULT_DESCRIPTION, seed=1, max_new_tokens=128)
        return {
            "already_loaded": False,
            "engine_id": ENGINE_ID,
            "model_loaded": True,
            "device": str(bundle.device),
            "sample_rate": 24000,
            "load_seconds": round(time.perf_counter() - started, 3),
            "profile": profile,
            "backend": "breeze-bf16-sdpa",
            "checkpoint_revision": CHECKPOINT_REVISION,
            "runtime_identity": self.runtime_identity,
        }

    def _runtime_identity(self):
        import torch
        import transformers

        bundle = self.bundle
        codec_config = bundle.codec.config
        if hasattr(codec_config, "to_dict"):
            codec_config = codec_config.to_dict()
        weight_path = Path(bundle.model_dir) / bundle.weights_name
        weight_stat = weight_path.stat()
        files = [
            LAB / "breeze_worker.py",
            LAB / "breeze_gen.py",
            LAB / "breeze" / "loader.py",
            LAB / "breeze" / "native.py",
            LAB / "breeze" / "runtime.py",
            LAB / "breeze" / "vendor" / "codec_model.py",
            LAB / "breeze" / "vendor" / "t5gemma2_compat.py",
        ]
        source_hash = hashlib.sha256()
        for path in files:
            source_hash.update(path.name.encode("utf-8"))
            source_hash.update(path.read_bytes())
        identity = {
            "checkpoint_revision": CHECKPOINT_REVISION,
            "weights_name": bundle.weights_name,
            "weights_size": weight_stat.st_size,
            "weights_mtime_ns": weight_stat.st_mtime_ns,
            "codec_config": codec_config,
            "decode_profile": bundle.decode_mode,
            "dtype": bundle.dtype_name,
            "attention": bundle.attention,
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "runtime_source_sha256": source_hash.hexdigest(),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _reference_path(path):
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise ValueError("Breeze requires a reference audio path under voice-lab/reference/.")
        resolved = resolve_reference_path(ENGINE_ID, path)
        if resolved.suffix.lower() not in REFERENCE_SUFFIXES:
            raise ValueError("Reference audio must be WAV, FLAC, or OGG.")
        return resolved

    def prepare_reference(self, path):
        if self.bundle is None:
            raise RuntimeError("Load Breeze before preparing a reference.")
        resolved = self._reference_path(path)
        digest = hashlib.sha256()
        with resolved.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        content_hash = digest.hexdigest()
        cache_key = f"{content_hash}:{self.runtime_identity}"
        cached = self.reference_cache.get(cache_key)
        if cached is not None:
            self.reference_cache.move_to_end(cache_key)
            return {"prepared": True, "engine_id": ENGINE_ID, "reference_sha256": content_hash,
                    "cache_hit": True, "runtime_identity": self.runtime_identity}

        codes = self.module.encode_reference(str(resolved))
        self.reference_cache[cache_key] = codes
        self.reference_cache.move_to_end(cache_key)
        while len(self.reference_cache) > MAX_REFERENCE_CACHE_ENTRIES:
            self.reference_cache.popitem(last=False)
        return {"prepared": True, "engine_id": ENGINE_ID, "reference_sha256": content_hash,
                "cache_hit": False, "runtime_identity": self.runtime_identity}

    def _cached_reference(self, path):
        prepared = self.prepare_reference(path)
        key = f"{prepared['reference_sha256']}:{self.runtime_identity}"
        return self.reference_cache[key], prepared

    def generate(self, params):
        if self.bundle is None:
            raise RuntimeError("Load Breeze before generation.")
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string.")
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters.")

        mode = str(params.get("mode", "design"))
        if mode not in {"design", "clone", "direction"}:
            raise ValueError("Breeze mode must be design, clone, or direction.")
        profile = str(params.get("profile", self.profile))
        if profile != self.profile:
            raise ValueError(f"Breeze worker profile is {self.profile!r}; requested {profile!r}.")
        instruction = str(params.get("voice_description", "") or "").strip()
        direction = str(params.get("direction", "") or "").strip()
        if mode == "design" and not instruction:
            raise ValueError("Voice design requires a non-empty voice_description.")
        if len(instruction) > 1000 or len(direction) > 1000:
            raise ValueError("Voice descriptions and directions must be 1000 characters or fewer.")
        if mode == "direction" and not direction:
            raise ValueError("Voice direction requires a non-empty direction.")

        cfg_default = 1.0 if mode == "clone" else 4.0
        sampling = {
            "cfg_scale": _number(params, "cfg_scale", cfg_default, 0.0, 20.0),
            "seed": _integer(params, "seed", 42, 1, 2_147_483_647),
            "max_new_tokens": _integer(params, "max_new_tokens", 1500, 64, 2047),
            "temperature": _number(params, "temperature", 0.9, 0.05, 5.0),
            "top_k": _integer(params, "top_k", 50, 0, 4096),
            "top_p": _number(params, "top_p", 1.0, 0.01, 1.0),
            "repetition_penalty": _number(params, "repetition_penalty", 1.1, 0.01, 10.0),
            "depth_temperature": _number(params, "depth_temperature", 0.9, 0.05, 5.0),
            "depth_top_k": _integer(params, "depth_top_k", 50, 0, 4096),
            "depth_top_p": _number(params, "depth_top_p", 1.0, 0.01, 1.0),
        }
        started = time.perf_counter()
        reference_meta = None
        if mode == "design":
            wav, sample_rate = self.module.design_speech(text.strip(), instruction, **sampling)
        else:
            reference_text = params.get("reference_text")
            if not isinstance(reference_text, str) or not reference_text.strip():
                raise ValueError("Clone and direction modes require the reference transcript in reference_text.")
            if len(reference_text) > 1200:
                raise ValueError("Reference transcript must be 1200 characters or fewer.")
            ref_codes, reference_meta = self._cached_reference(params.get("reference_path"))
            wav, sample_rate = self.module.clone_speech(
                text.strip(), ref_codes, reference_text.strip(),
                direction if mode == "direction" else None,
                **sampling,
            )

        info = {
            "total_s": time.perf_counter() - started,
            "first_chunk_s": None,
            "engine": ENGINE_ID,
            "mode": mode,
            "backend": "breeze-bf16-sdpa",
            "checkpoint_revision": CHECKPOINT_REVISION,
            "profile": self.profile,
            "generation": self.module.generation_info(),
            "sampling": sampling,
            "voice_description": instruction if mode == "design" else None,
            "direction": direction if mode == "direction" else None,
            "reference_sha256": reference_meta["reference_sha256"] if reference_meta else None,
            "reference_cache_hit": reference_meta["cache_hit"] if reference_meta else None,
            "runtime_identity": self.runtime_identity,
        }
        return wav, sample_rate, info

    def unload(self):
        before = self.bundle is not None
        self.reference_cache.clear()
        if self.bundle is not None:
            from breeze.loader import unload_breeze_bundle
            unload_breeze_bundle(self.bundle, reason="voice-lab worker unload", hard=True)
            self.module._bundle = None
        self.bundle = None
        self.module = None
        self.profile = None
        self.runtime_identity = None
        return {"model_loaded": False, "was_loaded": before, "reference_cache_entries": 0}
