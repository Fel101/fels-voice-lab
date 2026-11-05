"""Pinned local VoxCPM2 adapter for the isolated voice-lab worker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time

import numpy as np

from common import LAB, require_ram
from ram_needs import MIN_FREE_RAM_GB
from reference_paths import resolve_reference_path

SOURCE = LAB / "out" / "voxcpm2" / "source" / "src"
# A .venv-voxcpm made by hand without PyTorch borrows these packages, read-only (engine_worker.py adds them to its import path):
# the setup the Lab was first checked with, on an AMD card. tools/install_voxcpm.py gives .venv-voxcpm packages of its own.
BASE_SITE = LAB / ".venv-fqwen" / "Lib" / "site-packages"
MODEL = LAB / "hf-cache" / "models--openbmb--VoxCPM2" / "snapshots" / "32279effe8c19989596f05d353d1447f51d9e915"
SOURCE_COMMIT = "f772e498a45fbb5fb8e13fbf9b9c48be9fe33e69"
MODEL_REVISION = "32279effe8c19989596f05d353d1447f51d9e915"


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


# bfloat16 weights and activations peaked at 5.6 GiB allocated when this was measured; an 8 GB card reports a little under 8 GiB.
MIN_GPU_GIB = 7


def check_gpu_memory(device_name, total_bytes):
    """Stop on a GPU too small for the model (an integrated one, say), whatever the card is called."""
    gib = total_bytes / 1024 ** 3
    if gib < MIN_GPU_GIB:
        raise RuntimeError(f"VoxCPM2 needs a GPU with at least {MIN_GPU_GIB} GB of memory; {device_name} has {gib:.1f} GB. "
                           "Choose another GPU (see Hardware and state settings in docs/SETUP.md) or use another engine.")


# A take that runs on. VoxCPM2 sometimes keeps generating after the text ends, until it reaches its length cap: six audio
# patches of 0.16 s for each text token it was given, plus ten (VoxCPM's retry_badcase_ratio_threshold is the six). The Lab
# puts a voice description or a direction in front of the text, and VoxCPM counts those tokens too, so they raised the cap:
# the Models page's check sentence with seed 42 ran on to 20.8 s on an AMD Radeon AI PRO R9700, which is 130 patches, the
# cap for its 20 tokens with the description. The Lab gives VoxCPM a ratio that makes the cap that of the spoken text alone,
# treats a take of six patches a spoken token or more as one that ran on (VoxCPM's own rule for retry_badcase), and tries
# again from a seed derived from the requested one: three tries in all, as VoxCPM's retry_badcase_max_times. VoxCPM's own
# retry stays off because it tries the next seed, the one Perform gives its next take.
RUN_ON_PATCHES_PER_TOKEN = 6.0
RUN_ON_TRIES = 3


def token_count(tokenizer, text):
    """How many text tokens VoxCPM counts for `text`, after the whitespace clean-up its generate does first."""
    return max(1, len(tokenizer(re.sub(r"\s+", " ", text.replace("\n", " ")))))


def run_on_ratio(model_tokens, spoken_tokens):
    """The retry_badcase_ratio_threshold that turns VoxCPM's length cap, int(model_tokens * ratio + 10) patches, into the
    cap of the spoken tokens alone."""
    return RUN_ON_PATCHES_PER_TOKEN * spoken_tokens / model_tokens


def retry_seed(seed, attempt):
    """The seed of try `attempt`, where try 0 is the requested seed. The others are derived from it, so the same request
    makes the same tries, and are not seed + 1, which Perform gives its next take."""
    if attempt == 0:
        return seed
    digest = hashlib.sha256(f"voxcpm2-run-on:{seed}:{attempt}".encode("ascii")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def generate_until_it_ends(generate_once, seed, limit_seconds, sample_rate):
    """Make a take with generate_once(seed) -> waveform, and make it again from a derived seed while a take runs on, which
    is reaching limit_seconds. Returns (waveform, the seed it came from, the tries as {seed, seconds}). When all
    RUN_ON_TRIES takes run on it raises instead: a take that ran on past its text is not a finished take."""
    tries = []
    for attempt in range(RUN_ON_TRIES):
        attempt_seed = retry_seed(seed, attempt)
        waveform = generate_once(attempt_seed)
        seconds = waveform.size / sample_rate
        tries.append({"seed": attempt_seed, "seconds": round(seconds, 3)})
        if seconds < limit_seconds:
            return waveform, attempt_seed, tries
    seeds = ", ".join(str(item["seed"]) for item in tries)
    raise RuntimeError(f"VoxCPM2 kept going past the end of the text in all {RUN_ON_TRIES} tries (seeds {seeds}), so there "
                       "is no take. Try another seed or other wording.")


class VoxCPMWorker:
    def __init__(self, device):
        self.device = device
        self.model = None
        self.sample_rate = None
        self.patch_seconds = None
        self.runtime_identity = None

    @staticmethod
    def _reference(path):
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise ValueError("VoxCPM2 requires a local reference path for clone/continuation mode.")
        resolved = resolve_reference_path("voxcpm2", path)
        if resolved.suffix.lower() not in {".wav", ".flac", ".ogg"}:
            raise ValueError("VoxCPM2 references must be WAV, FLAC, or OGG audio.")
        return resolved

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def load(self):
        if self.model is not None:
            return self._load_info(already_loaded=True)
        if self.device != "cuda":
            raise RuntimeError("VoxCPM2 runs only on a GPU, and this service is using the CPU. "
                               "Start it without --device cpu, or use another engine.")
        require_ram(MIN_FREE_RAM_GB["voxcpm2"], "VoxCPM2")
        if not SOURCE.is_dir() or not MODEL.is_dir():
            raise RuntimeError("VoxCPM2's pinned source or model files are missing. Install it with Install on its card in "
                               "Models & features, or setup-voxcpm.bat; docs/SETUP.md says more.")

        # Only this dedicated worker process sees the pinned source (and, for a .venv-voxcpm without PyTorch, the packages it
        # borrows, which engine_worker.py added).
        if str(SOURCE) not in os.sys.path:
            os.sys.path.insert(0, str(SOURCE))
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "MODELSCOPE_OFFLINE"):
            os.environ[name] = "1"

        import torch
        from voxcpm import VoxCPM

        if not torch.cuda.is_available():
            raise RuntimeError(f"VoxCPM2 runs only on a graphics card, and this environment's PyTorch ({torch.__version__}) "
                               "sees none it can use.")
        properties = torch.cuda.get_device_properties(0)
        device_name = properties.name
        check_gpu_memory(device_name, properties.total_memory)
        if torch.version.hip:
            torch.backends.cudnn.enabled = False    # MIOpen, which ROCm puts behind this switch, failed on the AMD card it was checked on
        started = time.perf_counter()
        self.model = VoxCPM.from_pretrained(
            str(MODEL), load_denoiser=False, optimize=False,
            device="cuda", local_files_only=True,
        )
        tts = self.model.tts_model
        self.sample_rate = int(tts.sample_rate)
        # One patch is patch_size latent frames, each one encoder hop of audio: 4 x 640 samples at 16 kHz, 0.16 s.
        self.patch_seconds = tts.patch_size * tts.audio_vae.chunk_size / tts.audio_vae.sample_rate
        self.runtime_identity = self._identity(torch)
        info = self._load_info(already_loaded=False)
        info.update({
            "load_seconds": round(time.perf_counter() - started, 4),
            "device_name": device_name,
            "torch": str(torch.__version__),
            "hip": torch.version.hip,
            "dtype": str(getattr(self.model.tts_model.config, "dtype", "unknown")),
        })
        return info

    def _identity(self, torch):
        source_hash = hashlib.sha256()
        for path in (LAB / "voxcpm_worker.py", LAB / "engine_worker.py", LAB / "worker_runtime.py"):
            source_hash.update(path.name.encode("utf-8"))
            source_hash.update(path.read_bytes())
        payload = {
            "engine_id": "voxcpm2", "source_commit": SOURCE_COMMIT,
            "model_revision": MODEL_REVISION, "torch": str(torch.__version__),
            "hip": torch.version.hip, "device": self.device,
            "source_sha256": source_hash.hexdigest(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _load_info(self, *, already_loaded):
        return {
            "already_loaded": already_loaded, "engine_id": "voxcpm2",
            "model_loaded": self.model is not None, "device": self.device,
            "sample_rate": self.sample_rate, "backend": "voxcpm2-local-source",
            "backend_version": SOURCE_COMMIT, "checkpoint": "openbmb/VoxCPM2",
            "checkpoint_revision": MODEL_REVISION, "mode": "design,clone,continuation",
            "runtime_identity": self.runtime_identity,
        }

    def prepare_reference(self, params):
        if self.model is None:
            raise RuntimeError("Load VoxCPM2 before preparing a reference.")
        path = self._reference(params.get("path"))
        return {
            "prepared": True, "engine_id": "voxcpm2", "path": str(path),
            "reference_sha256": self._hash(path), "runtime_identity": self.runtime_identity,
            "cache_hit": False,
            "note": "VoxCPM2 encodes the reference during generation; this operation validates and fingerprints it.",
        }

    def generate(self, params):
        if self.model is None:
            raise RuntimeError("Load VoxCPM2 before generation.")
        common_fields = {"text", "mode", "seed", "cfg_value", "inference_timesteps"}
        mode = params.get("mode", "design")
        mode_fields = {
            "design": {"voice_description"},
            "clone": {"reference_path", "direction"},
            "continuation": {"reference_path", "prompt_path", "prompt_text"},
        }
        if mode not in mode_fields:
            raise ValueError("VoxCPM2 mode must be design, clone, or continuation.")
        extra = set(params) - common_fields - mode_fields[mode] - {"output_file"}
        if extra:
            raise ValueError(f"Unsupported VoxCPM2 {mode} parameter(s): {', '.join(sorted(extra))}.")
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string.")
        text = text.strip()
        if len(text) > 1200:
            raise ValueError("Keep it under 1200 characters.")
        direction = params.get("direction", "")
        if not isinstance(direction, str) or len(direction) > 1000:
            raise ValueError("Direction must be text of 1000 characters or fewer.")
        direction = direction.strip()
        seed = _integer(params, "seed", 42, 0, 2_147_483_647)
        cfg_value = _number(params, "cfg_value", 2.0, 0.1, 10.0)
        steps = _integer(params, "inference_timesteps", 10, 1, 100)
        call = {
            "text": text, "cfg_value": cfg_value,
            "inference_timesteps": steps, "seed": seed,
            "retry_badcase": False, "normalize": False,
        }
        reference_meta = None
        reference_path = None
        prompt_path = None
        prompt_text = None
        voice_description = None
        if mode == "design":
            voice_description = params.get("voice_description")
            if not isinstance(voice_description, str) or not voice_description.strip():
                raise ValueError("VoxCPM2 design requires a non-empty voice_description.")
            voice_description = voice_description.strip()
            if len(voice_description) > 1000:
                raise ValueError("Voice descriptions must be 1000 characters or fewer.")
            # The pinned public design examples use parenthesized natural-language
            # descriptions as a prefix in the actual model input.
            call["text"] = f"({voice_description}){text}"
        elif mode == "clone":
            reference_path = self._reference(params.get("reference_path"))
            reference_meta = self.prepare_reference({"path": str(reference_path)})
            call["reference_wav_path"] = str(reference_path)
            if direction:
                # The model's documented free-form voice design control is a
                # parenthesized prefix. Acting on a clone is treated as an
                # experimental reuse of that prompt convention.
                call["text"] = f"({direction}){text}"
        else:
            reference_path = self._reference(params.get("reference_path"))
            prompt_path = self._reference(params.get("prompt_path"))
            prompt_text = params.get("prompt_text")
            if not isinstance(prompt_text, str) or not prompt_text.strip():
                raise ValueError("VoxCPM2 continuation requires the exact prompt_text for prompt_path.")
            if len(prompt_text.strip()) > 1200:
                raise ValueError("Continuation prompt_text must be 1200 characters or fewer.")
            call.update({
                "reference_wav_path": str(reference_path),
                "prompt_wav_path": str(prompt_path),
                "prompt_text": prompt_text.strip(),
            })

        # Cap the take by the spoken text, without the description or direction in front of it (see RUN_ON_TRIES).
        tts = self.model.tts_model
        spoken_tokens = token_count(tts.text_tokenizer, text)
        call["retry_badcase_ratio_threshold"] = run_on_ratio(token_count(tts.text_tokenizer, call["text"]), spoken_tokens)
        limit_seconds = spoken_tokens * RUN_ON_PATCHES_PER_TOKEN * self.patch_seconds

        def generate_once(attempt_seed):
            waveform = np.asarray(self.model.generate(**{**call, "seed": attempt_seed}), dtype=np.float32).reshape(-1)
            if waveform.size == 0 or not np.isfinite(waveform).all():
                raise RuntimeError("VoxCPM2 produced empty or non-finite audio.")
            actual_seed = getattr(tts, "last_successful_seed", None)
            if actual_seed != attempt_seed:
                raise RuntimeError(f"VoxCPM2 used seed {actual_seed!r}; requested seed was {attempt_seed}.")
            return waveform

        import torch
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        waveform, used_seed, tries = generate_until_it_ends(generate_once, seed, limit_seconds, self.sample_rate)
        elapsed = time.perf_counter() - started
        info = {
            "total_s": elapsed, "first_chunk_s": None, "engine": "voxcpm2",
            "mode": mode, "backend": "voxcpm2-local-source", "backend_version": SOURCE_COMMIT,
            "checkpoint": "openbmb/VoxCPM2", "checkpoint_revision": MODEL_REVISION,
            "runtime_identity": self.runtime_identity, "device": self.device,
            "sample_rate": self.sample_rate, "channels": 1,
            "direction": direction or None,
            "direction_status": "experimental_parenthesized_prefix_on_clone" if direction and mode == "clone" else (
                "design_prompt_prefix" if voice_description else None
            ),
            "voice_description": voice_description,
            "reference_sha256": reference_meta["reference_sha256"] if reference_meta else None,
            "reference_path": str(reference_path) if reference_path else None,
            "prompt_path": str(prompt_path) if prompt_path else None,
            "prompt_text": prompt_text,
            "effective_controls": {
                "seed": used_seed, "cfg_value": cfg_value,
                "inference_timesteps": steps, "retry_badcase": False,
                "normalize": False,
            },
            # The seed asked for, the longest a take of this text may be before it counts as running on, and each try:
            # a take that ran on was made again from a derived seed, which effective_controls names.
            "requested_seed": seed,
            "run_on": {"limit_seconds": round(limit_seconds, 3), "tries": tries},
            "peak_vram_bytes": int(torch.cuda.max_memory_allocated()),
        }
        return waveform, self.sample_rate, info

    def unload(self):
        import torch
        self.model = None
        self.sample_rate = None
        self.patch_seconds = None
        self.runtime_identity = None
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        return {"model_loaded": False}
