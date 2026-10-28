"""Worker-side wrapper around the installed Chatterbox voice-conversion model."""

from __future__ import annotations

import importlib.metadata
import platform
from pathlib import Path
import time

from common import LAB, to_numpy
from reference_paths import configured_worker_roots, CONVERSION_SOURCE_ROOTS_ENV


CHECKPOINT = "ResembleAI/chatterbox"
CHECKPOINT_REVISION = "5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18"
SNAPSHOT = (LAB / "hf-cache" / "hub" / "models--ResembleAI--chatterbox" /
            "snapshots" / CHECKPOINT_REVISION)
ALLOWED_ROOTS = tuple((LAB / relative).resolve() for relative in (
    "reference", "data/voices", "data/cast/media/references", "data/cast/media/takes",
    "out/conversions", "out/scenes",
))
ALLOWED_AUDIO = {".wav", ".flac", ".ogg"}


def _resolve_audio(value, role):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{role} audio path is required.")
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() not in ALLOWED_AUDIO or not path.is_file():
        raise ValueError(f"{role} audio must be an existing WAV, FLAC, or OGG file.")
    roots = (*ALLOWED_ROOTS, *configured_worker_roots())
    if role == "Source performance":
        roots += configured_worker_roots(CONVERSION_SOURCE_ROOTS_ENV)
    if not any(root in path.parents for root in roots):
        raise ValueError(f"{role} audio must be inside voice-lab reference, voice, cast-media, scene, or voice conversion output storage.")
    return path


class ChatterboxVCWorker:
    """Load only the already-cached checkpoint; conversion stays separate from text synthesis."""

    def __init__(self, device):
        self.device = device
        self.model = None
        self.sample_rate = None

    def load(self):
        if self.model is not None:
            return {"already_loaded": True, "sample_rate": self.sample_rate,
                    "checkpoint": CHECKPOINT, "checkpoint_revision": CHECKPOINT_REVISION,
                    "mode": "voice_conversion"}
        required = ("s3gen.safetensors", "conds.pt")
        missing = [name for name in required if not (SNAPSHOT / name).is_file()]
        if missing:
            raise RuntimeError(f"Chatterbox VC checkpoint files are missing from pinned local snapshot {SNAPSHOT}: "
                               + ", ".join(missing) + ". Restore the existing chatterbox cache before retrying.")
        from chatterbox.vc import ChatterboxVC
        import torch

        started = time.perf_counter()
        self.model = ChatterboxVC.from_local(str(SNAPSHOT), self.device)
        self.sample_rate = int(self.model.sr)
        return {
            "already_loaded": False, "sample_rate": self.sample_rate,
            "checkpoint": CHECKPOINT, "checkpoint_revision": CHECKPOINT_REVISION,
            "backend": "chatterbox-vc", "backend_version": importlib.metadata.version("chatterbox-tts"),
            "mode": "voice_conversion", "runtime_identity": {
                "python": platform.python_version(), "torch": torch.__version__,
            },
            "device_name": torch.cuda.get_device_name() if torch.cuda.is_available() else "cpu",
            "torch": torch.__version__, "hip": getattr(torch.version, "hip", None),
            "load_seconds": round(time.perf_counter() - started, 3),
            "watermark": "PerthImplicitWatermarker applied by installed ChatterboxVC.generate",
        }

    def generate(self, params):
        if self.model is None:
            raise RuntimeError("Load the Chatterbox VC model before conversion.")
        import soundfile as sf

        source = _resolve_audio(params.get("source_path"), "Source performance")
        target = _resolve_audio(params.get("target_reference_path"), "Target voice reference")
        if source == target:
            raise ValueError("Source performance and target voice reference must be separate files.")
        source_info, target_info = sf.info(source), sf.info(target)
        if source_info.duration <= 0 or source_info.duration > 60:
            raise ValueError("Source performance must be non-empty and no longer than 60 seconds.")
        if target_info.duration <= 0 or target_info.duration > 10:
            raise ValueError("Target reference must be non-empty and no longer than 10 seconds.")
        started = time.perf_counter()
        audio = self.model.generate(audio=str(source), target_voice_path=str(target))
        wav = to_numpy(audio)
        return wav, self.sample_rate, {
            "total_s": round(time.perf_counter() - started, 4),
            "backend": "chatterbox-vc", "backend_version": importlib.metadata.version("chatterbox-tts"),
            "checkpoint": CHECKPOINT, "checkpoint_revision": CHECKPOINT_REVISION,
            "mode": "voice_conversion", "sample_rate": self.sample_rate,
            "source_duration_seconds": round(float(source_info.duration), 4),
            "target_reference_duration_seconds": round(float(target_info.duration), 4),
            "watermark": "PerthImplicitWatermarker",
        }

    def unload(self):
        before = self.model is not None
        self.model = None
        self.sample_rate = None
        return {"model_released": before}
