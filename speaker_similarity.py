"""Proxy speaker similarity from Chatterbox's speaker encoder.

The encoder is the one Chatterbox already ships (chatterbox/models/voice_encoder). It runs on
CPU and only reads local weights. The score is the cosine similarity of two speaker embeddings
in [-1, 1]. It is a proxy: it measures how alike two voices sound to this encoder, not whether
a listener would call them the same person. The two bands that label a score are in
catalog/speaker_similarity.json, with the clip statistics they were measured from; read them before trusting a number.
"""

from __future__ import annotations

import json
from importlib.util import find_spec
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

LAB = Path(__file__).resolve().parent
ENCODER_WEIGHTS = (LAB / "hf-cache" / "hub" / "models--ResembleAI--chatterbox-turbo" / "snapshots"
                   / "749d1c1a46eb10492095d68fbcf55691ccf137cd" / "ve.safetensors")
_lock = threading.Lock()
_encoder = None


def encoder_available():
    return ENCODER_WEIGHTS.is_file() and all(find_spec(name) is not None
        for name in ("torch", "safetensors", "chatterbox"))


def _load_encoder():
    global _encoder
    with _lock:
        if _encoder is None:
            import torch
            from safetensors.torch import load_file
            from chatterbox.models.voice_encoder import VoiceEncoder

            if not ENCODER_WEIGHTS.is_file():
                raise FileNotFoundError(f"Speaker encoder weights are missing: {ENCODER_WEIGHTS}")
            encoder = VoiceEncoder()
            encoder.load_state_dict(load_file(str(ENCODER_WEIGHTS)))
            encoder.eval()
            _encoder = (encoder.to(torch.device("cpu")), torch)
        return _encoder


ENCODER_RATE = 16_000


def _to_encoder_rate(audio, rate):
    # Resample here with SciPy: the encoder's own path needs resampy, which this environment lacks.
    from math import gcd
    from scipy.signal import resample_poly

    if rate == ENCODER_RATE:
        return audio
    factor = gcd(ENCODER_RATE, rate)
    return resample_poly(audio, ENCODER_RATE // factor, rate // factor).astype(np.float32)


def embedding_from_samples(audio, rate):
    """Unit-length speaker embedding for samples already in memory (mono, or channels in the columns)."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    audio = _to_encoder_rate(audio, rate)
    encoder, torch = _load_encoder()
    with torch.inference_mode():
        vector = encoder.embeds_from_wavs([audio], ENCODER_RATE, as_spk=True)
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    return vector / np.linalg.norm(vector)


def embedding(path):
    """Unit-length speaker embedding for one audio file."""
    audio, rate = sf.read(str(path), dtype="float32", always_2d=False)
    return embedding_from_samples(audio, rate)


def similarity(path_a, path_b):
    """Cosine similarity between two clips' speaker embeddings."""
    return float(np.dot(embedding(path_a), embedding(path_b)))


def voice_match(cast_store, take_id):
    """Proxy similarity between a Cast take and the reference voice it was generated from.

    Preset (CustomVoice) takes have no reference clip, so there is nothing to compare against.
    """
    take = cast_store.get_take(take_id)
    reference_id = take.get("reference_version_id")
    if not reference_id:
        return {"available": False,
                "reason": "This take has no reference clip to compare against (preset voice)."}
    if not encoder_available():
        return {"available": False, "reason": "The voice-match encoder is not installed. See docs/SETUP.md."}
    score = similarity(cast_store.get_media_path("reference", reference_id),
                       cast_store.get_media_path("take", take_id))
    return {"available": True, "score": round(score, 4), "band": band(score),
            "reference_version_id": reference_id, "proxy": True,
            "calibration": "bands measured on one target voice (af_heart); see catalog/speaker_similarity.json"}


BANDS_FILE = LAB / "catalog" / "speaker_similarity.json"
_bands = None


def _measured_bands():
    global _bands
    if _bands is None:
        if not BANDS_FILE.is_file():
            raise FileNotFoundError(
                f"The speaker-similarity bands are missing: {BANDS_FILE}. The file ships with the lab; restore it from the repository.")
        _bands = json.loads(BANDS_FILE.read_text(encoding="utf-8"))["bands"]
    return _bands


def band(score):
    """Label a similarity with the measured bands: likely_same, uncertain, or likely_different."""
    bands = _measured_bands()
    if score >= bands["likely_same_voice_at_or_above"]:
        return "likely_same"
    if score < bands["likely_different_voice_below"]:
        return "likely_different"
    return "uncertain"
