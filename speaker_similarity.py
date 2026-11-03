"""Proxy speaker similarity from Chatterbox Turbo's speaker encoder.

The encoder is the small network Chatterbox ships (chatterbox/models/voice_encoder): a three-layer LSTM over 40 mel bands that
turns a clip into a 256-number speaker embedding. It runs here with NumPy alone, from Turbo's weights (ve.safetensors in the
pinned hf-cache snapshot, which tools/install_chatterbox.py fetches), so the voice match works in the lab's own environment while
Chatterbox itself runs in .venv-chatterbox. It follows Chatterbox's own path step for step: librosa's 20 dB trim, a 400-point
STFT and a Slaney mel bank, partials of 160 frames 77 frames apart, and the mean of their embeddings. tests/test_speaker_similarity.py
holds it to embeddings that Chatterbox 0.1.7 made, so the bands below still describe its scores.

The score is the cosine similarity of two speaker embeddings in [-1, 1]. It is a proxy: it measures how alike two voices sound
to this encoder, not whether a listener would call them the same person. The two bands that label a score are in
catalog/speaker_similarity.json, with the clip statistics they were measured from; read them before trusting a number.

The encoder's design comes from Real-Time-Voice-Cloning (MIT, Corentin Jemine) by way of Chatterbox (MIT, Resemble AI), and
the trim and mel steps follow librosa (ISC); THIRD_PARTY_NOTICES.md and licenses/ keep their notices.
"""

from __future__ import annotations

import json
import threading
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import get_window, resample_poly
from scipy.special import expit

from chatterbox_support import turbo_snapshot_dir

LAB = Path(__file__).resolve().parent
ENCODER_WEIGHTS = turbo_snapshot_dir(LAB) / "ve.safetensors"
ENCODER_RATE = 16_000
# Chatterbox's VoiceEncConfig, and the defaults of the calls it makes.
MEL_BANDS, FFT_SIZE, HOP, MEL_TOP_HZ = 40, 400, 160, 8000
PARTIAL_FRAMES, MIN_COVERAGE = 160, 0.8
PARTIAL_STEP = int(np.round((ENCODER_RATE / 1.3) / PARTIAL_FRAMES))    # 77: Resemble's rate of 1.3 partials a second
TRIM_TOP_DB, TRIM_FRAME, TRIM_HOP = 20, 2048, 512
_lock = threading.Lock()
_encoder = None


def encoder_available():
    return ENCODER_WEIGHTS.is_file()


def read_safetensors(path):
    """The float32 tensors of a safetensors file: an 8-byte header length, a JSON header, then the data."""
    data = Path(path).read_bytes()
    size = int.from_bytes(data[:8], "little")
    header = json.loads(data[8:8 + size])
    tensors = {}
    for name, item in header.items():
        if name == "__metadata__":
            continue
        if item["dtype"] != "F32":
            raise ValueError(f"{Path(path).name}: {name} is {item['dtype']}, and the encoder reads float32 only.")
        start, end = item["data_offsets"]
        tensors[name] = np.frombuffer(data, dtype="<f4", count=(end - start) // 4,
                                      offset=8 + size + start).reshape(item["shape"])
    return tensors


def _mel_scale(hz):
    """librosa.hz_to_mel with Slaney's scale: linear below 1 kHz, logarithmic above."""
    hz = np.asarray(hz, dtype=np.float64)
    return np.where(hz >= 1000.0, 15.0 + np.log(np.maximum(hz, 1e-9) / 1000.0) / (np.log(6.4) / 27.0), hz / (200.0 / 3))


def _hz_scale(mel):
    return np.where(mel >= 15.0, 1000.0 * np.exp(np.log(6.4) / 27.0 * (mel - 15.0)), mel * (200.0 / 3))


def mel_bank():
    """librosa.filters.mel(sr=16000, n_fft=400, n_mels=40, fmax=8000): Slaney triangles, area-normalised, in float32."""
    edges = _hz_scale(np.linspace(_mel_scale(0.0), _mel_scale(MEL_TOP_HZ), MEL_BANDS + 2))
    ramps = np.subtract.outer(edges, np.fft.rfftfreq(FFT_SIZE, 1.0 / ENCODER_RATE))
    widths = np.diff(edges)
    weights = np.zeros((MEL_BANDS, FFT_SIZE // 2 + 1), dtype=np.float32)
    for band in range(MEL_BANDS):
        weights[band] = np.maximum(0, np.minimum(-ramps[band] / widths[band], ramps[band + 2] / widths[band + 1]))
    weights *= (2.0 / (edges[2:] - edges[:-2]))[:, np.newaxis]
    return weights


def _frames(signal, length, hop):
    count = 1 + (len(signal) - length) // hop
    return np.lib.stride_tricks.as_strided(signal, (count, length), (signal.strides[0] * hop, signal.strides[0]))


def trim(audio):
    """librosa.effects.trim(audio, top_db=20): drop the frames at either end that are 20 dB below the loudest one."""
    padded = np.pad(audio, TRIM_FRAME // 2)
    rms = np.sqrt(np.mean(np.square(_frames(padded, TRIM_FRAME, TRIM_HOP), dtype=np.float32), axis=1))
    power = np.square(rms)
    db = 10.0 * np.log10(np.maximum(1e-10, power)) - 10.0 * np.log10(np.maximum(1e-10, np.max(rms) ** 2))
    loud = np.flatnonzero(db > -TRIM_TOP_DB)
    if not loud.size:
        return audio[:0]
    return audio[loud[0] * TRIM_HOP:min(len(audio), (loud[-1] + 1) * TRIM_HOP)]


def mel_frames(audio):
    """Chatterbox's melspectrogram: power mel bands of a centred, reflect-padded Hann STFT, one row per 10 ms frame."""
    padded = np.pad(audio, FFT_SIZE // 2, mode="reflect")
    window = get_window("hann", FFT_SIZE, fftbins=True)
    spectrum = np.fft.rfft(window * _frames(padded, FFT_SIZE, HOP), axis=1).astype(np.complex64)
    power = np.abs(spectrum) ** 2
    return (mel_bank() @ power.T).T


def partials(mels):
    """The overlapping 160-frame windows the encoder reads, the last one padded with zeros as Chatterbox pads it."""
    count, remainder = divmod(max(len(mels) - PARTIAL_FRAMES + PARTIAL_STEP, 0), PARTIAL_STEP)
    if count == 0 or (remainder + PARTIAL_FRAMES - PARTIAL_STEP) / PARTIAL_FRAMES >= MIN_COVERAGE:
        count += 1
    needed = PARTIAL_FRAMES + PARTIAL_STEP * (count - 1)
    if needed > len(mels):
        mels = np.concatenate([mels, np.zeros((needed - len(mels), MEL_BANDS), dtype=mels.dtype)])
    return np.stack([mels[index * PARTIAL_STEP:index * PARTIAL_STEP + PARTIAL_FRAMES] for index in range(count)])


def _load_encoder():
    global _encoder
    with _lock:
        if _encoder is None:
            if not ENCODER_WEIGHTS.is_file():
                raise FileNotFoundError(f"Speaker encoder weights are missing: {ENCODER_WEIGHTS}")
            _encoder = read_safetensors(ENCODER_WEIGHTS)
        return _encoder


def encode(batch, weights):
    """The LSTM and projection of Chatterbox's VoiceEncoder.forward: unit-length embeddings for a batch of partials."""
    hidden_size = weights["lstm.weight_hh_l0"].shape[1]
    sequence = np.asarray(batch, dtype=np.float32)
    for layer in range(3):
        w_ih, w_hh = weights[f"lstm.weight_ih_l{layer}"], weights[f"lstm.weight_hh_l{layer}"]
        inputs = sequence @ w_ih.T + (weights[f"lstm.bias_ih_l{layer}"] + weights[f"lstm.bias_hh_l{layer}"])
        h = np.zeros((len(sequence), hidden_size), dtype=np.float32)
        c = np.zeros_like(h)
        outputs = np.empty((len(sequence), sequence.shape[1], hidden_size), dtype=np.float32)
        for step in range(sequence.shape[1]):
            gates = inputs[:, step] + h @ w_hh.T
            i, f, g, o = np.split(gates, 4, axis=1)    # PyTorch's gate order: input, forget, cell, output
            c = expit(f) * c + expit(i) * np.tanh(g)
            h = expit(o) * np.tanh(c)
            outputs[:, step] = h
        sequence = outputs
    raw = np.maximum(h @ weights["proj.weight"].T + weights["proj.bias"], 0)
    return raw / np.linalg.norm(raw, axis=1, keepdims=True)


def _to_encoder_rate(audio, rate):
    if rate == ENCODER_RATE:
        return audio
    factor = gcd(ENCODER_RATE, rate)
    return resample_poly(audio, ENCODER_RATE // factor, rate // factor).astype(np.float32)


def embedding_from_samples(audio, rate):
    """Unit-length speaker embedding for samples already in memory (mono, or channels in the columns)."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    audio = trim(np.ascontiguousarray(_to_encoder_rate(audio, rate), dtype=np.float32))
    if audio.size == 0:
        raise ValueError("The clip is empty, so it has no speaker embedding.")
    vector = encode(partials(mel_frames(audio)), _load_encoder()).mean(axis=0)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


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
        return {"available": False, "reason": "The voice-match encoder is not installed: it comes with Chatterbox Turbo. "
                                               "Install Turbo from Models & features, or run setup-chatterbox.bat."}
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
