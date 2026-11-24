"""Loudness and peak readings for a take: integrated loudness (LUFS), sample peak and true peak.

Integrated loudness is ITU-R BS.1770 as pyloudnorm computes it, so a take's number matches what a loudness meter reports
for the same file. It needs at least 0.4 s of audio and a signal above the -70 LUFS gate; a take with neither gets no
loudness figure and a note that says why. The sample peak is the highest sample. The true peak is the highest point of
the signal rebuilt at four times the sample rate, which is what a converter plays, so it can sit above every sample;
lossy export (MP3, AAC, Ogg) can clip when it does. The readings describe the audio and say nothing about the words,
the voice or the acting.
"""

import json
import math
import warnings

import numpy as np
import pyloudnorm
import soundfile as sf
from scipy.signal import resample_poly

METER = "loudness-v1"
MIN_SECONDS = 0.4
OVERSAMPLE = 4
# Above this true peak lossy encoders start to overshoot full scale (the EBU R128 recommendation).
TRUE_PEAK_WARNING_DBTP = -1.0


def _db(value):
    return round(20 * math.log10(value), 2) if value > 0 else None


def measure(audio, sample_rate):
    """The readings for samples (frames, or frames by channels) at sample_rate. Raises ValueError for unusable audio.
    `note` is None, or silent, too_short or too_quiet when there is no loudness figure."""
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2 or not data.size or not np.isfinite(data).all() or int(sample_rate) <= 0:
        raise ValueError("The audio is empty or contains samples that are not numbers.")
    sample_peak = float(np.max(np.abs(data)))
    record = {"status": "measured", "meter": METER, "integrated_lufs": None, "sample_peak_dbfs": _db(sample_peak),
              "true_peak_dbtp": None, "note": None}
    if sample_peak == 0.0:
        record["note"] = "silent"
        return record
    true_peak = float(np.max(np.abs(resample_poly(data, OVERSAMPLE, 1, axis=0))))
    record["true_peak_dbtp"] = _db(max(true_peak, sample_peak))
    if data.shape[0] / int(sample_rate) < MIN_SECONDS:
        record["note"] = "too_short"
        return record
    try:
        with warnings.catch_warnings(), np.errstate(divide="ignore"):
            warnings.simplefilter("ignore")
            lufs = float(pyloudnorm.Meter(int(sample_rate)).integrated_loudness(data))
    except ValueError:
        record["note"] = "too_short"
        return record
    if math.isfinite(lufs):
        record["integrated_lufs"] = round(lufs, 1)
    else:
        record["note"] = "too_quiet"
    return record


def measure_file(path):
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    return measure(audio, rate)


def unavailable(reason):
    """The record for audio that could not be read or measured. Storing it keeps a backfill from trying again forever."""
    return {"status": "unavailable", "meter": METER, "reason": str(reason)[:300]}


def stored(raw):
    """A take's saved record. A take with none reads as not measured, never as quiet or fine."""
    if not raw:
        return {"status": "not_measured", "meter": None}
    return json.loads(raw)
