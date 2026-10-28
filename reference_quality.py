"""Pre-save screen for a reference clip: a 0-100 score and plain warnings.

The screen reads the processed clip that would be saved, so a noise-reduction preview is judged the way a clone
would hear it. It looks for clipping, a high noise floor, reverberant decay, steady tones in the quieter parts and
a second voice. A warning means "listen before saving". Nothing here blocks a save, and the thresholds are
starting points: none has been calibrated against listening tests. The speaker check uses the proxy encoder and its
calibrated bands (speaker_similarity.py). A check that cannot run says why instead of passing.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import stft

LEVEL_FRAME_SECONDS = 0.01        # speech activity and decay are measured in 10 ms frames
FLOOR_WINDOW_SECONDS = 0.1        # the quiet floor is the 10th percentile of 100 ms windows, 10 ms apart
MIN_SECONDS = 1.0                 # shorter clips have too few windows for a floor
ACTIVE_ABOVE_FLOOR_DB = 15.0      # a frame this far above the floor is speech or sound
DECAY_BOTTOM_ABOVE_FLOOR_DB = 5.0 # a decay is timed from ACTIVE_ABOVE_FLOOR_DB down to this level (a 10 dB span)
DECAY_MAX_SECONDS = 1.0           # a tail that is still above the bottom after this long counts as this long
MIN_DECAYS = 3
FLOOR_WARN_DBFS = -50.0
SNR_WARN_DB = 20.0
RT60_WARN_SECONDS = 0.5           # 60 dB of decay estimated from the 10 dB span
QUIET_BELOW_SPEECH_DB = 12.0      # the quieter parts are frames this far below the speech level
MIN_QUIET_FRAMES = 20
TONE_WINDOW_SECONDS = 0.1
TONE_HOP_SECONDS = 0.05
TONE_PROMINENCE_DB = 15.0         # a bin this far above its neighbourhood ...
TONE_STABILITY_DB = 3.0           # ... that stays this steady across the quieter frames is a tone
TONE_NEIGHBOURHOOD_BINS = 51      # about 500 Hz at the 10 Hz resolution of a 0.1 s window
BLOCK_SECONDS = 3.0
MAX_BLOCKS = 4
CLIP_LEVEL = 0.999
HEAVY_CLIP_FRACTION = 0.001
PENALTIES = {"clipping": 15, "noise_floor": 15, "reverb": 15, "background_tones": 15, "speakers": 30}
HEAVY_CLIPPING_PENALTY = 25
ACOUSTIC_CHECKS = ("noise_floor", "reverb", "background_tones", "speakers")
METHOD = ("heuristic screen of the processed clip; thresholds are starting points, not listening-test calibrations; "
          "the speaker check uses the proxy encoder's calibrated bands")


def _frame_db(samples, frame):
    """RMS in dBFS of consecutive frames; a partial frame at the end is dropped."""
    count = len(samples) // frame
    blocks = np.asarray(samples[: count * frame], dtype=np.float64).reshape(count, frame)
    return np.maximum(20 * np.log10(np.sqrt(np.mean(blocks ** 2, axis=1)) + 1e-6), -120.0)


def _window_db(samples, window, hop):
    """RMS in dBFS of windows that start every ``hop`` samples, so short pauses still show a quiet window."""
    squares = np.asarray(samples, dtype=np.float64) ** 2
    cumulative = np.concatenate(([0.0], np.cumsum(squares)))
    starts = np.arange(0, len(squares) - window + 1, hop)
    means = (cumulative[starts + window] - cumulative[starts]) / window
    return np.maximum(10 * np.log10(means + 1e-12), -120.0)


def _sample_mask(frames, frame, total):
    """Expand a per-frame mask to samples, frames counted from the start of the clip."""
    mask = np.zeros(total, dtype=bool)
    mask[: len(frames) * frame] = np.repeat(frames, frame)
    return mask


def _not_checked(reason):
    return {"status": "not_checked", "reason": reason}


def _clipping(samples):
    clipped = int(np.count_nonzero(np.abs(samples) >= CLIP_LEVEL))
    check = {"status": "warn" if clipped else "pass", "clipped_samples": clipped,
             "heavy": clipped / len(samples) > HEAVY_CLIP_FRACTION}
    if clipped:
        check["message"] = f"Clipping: {clipped} samples reach full scale."
    return check


def _noise_floor(floor_db, speech_db):
    if speech_db is None:
        return _not_checked("no speech above the noise floor was found")
    snr = speech_db - floor_db
    check = {"status": "pass", "floor_dbfs": round(floor_db, 1), "speech_dbfs": round(speech_db, 1),
             "snr_db": round(snr, 1)}
    if floor_db > FLOOR_WARN_DBFS or snr < SNR_WARN_DB:
        check["status"] = "warn"
        check["message"] = f"Noise: the floor is {floor_db:.0f} dBFS and speech sits {snr:.0f} dB above it."
    return check


def _decay_seconds(levels, active, floor_db):
    """Time each speech burst's tail takes to fall from the active threshold to the bottom of the decay span."""
    limit = round(DECAY_MAX_SECONDS / LEVEL_FRAME_SECONDS)
    bottom = floor_db + DECAY_BOTTOM_ABOVE_FLOOR_DB
    times = []
    for end in np.flatnonzero(active[:-1] & ~active[1:]):
        start = int(end) + 1
        stop = min(len(levels), start + limit)
        for offset, frame in enumerate(range(start, stop)):
            if active[frame]:
                break                       # speech resumed before the tail reached the bottom
            if levels[frame] <= bottom:
                times.append(offset * LEVEL_FRAME_SECONDS)
                break
        else:
            if stop - start == limit:
                times.append(DECAY_MAX_SECONDS)  # the tail outlasted the window
    return times


def _reverb(levels, active, floor_db):
    times = _decay_seconds(levels, active, floor_db)
    if len(times) < MIN_DECAYS:
        return _not_checked(f"fewer than {MIN_DECAYS} clean decays after speech")
    rt60 = 6 * float(np.median(times))      # 60 dB is six 10 dB spans
    check = {"status": "pass", "rt60_seconds": round(rt60, 3), "decays": len(times)}
    if rt60 >= RT60_WARN_SECONDS:
        check["status"] = "warn"
        check["message"] = f"Reverb: speech tails suggest about {rt60:.2f} s RT60 (an estimate)."
    return check


def _background_tones(samples, rate, speech_db):
    if speech_db is None:
        return _not_checked("no speech above the noise floor was found")
    window = max(256, round(TONE_WINDOW_SECONDS * rate))
    hop = max(1, round(TONE_HOP_SECONDS * rate))
    if len(samples) < window:
        return _not_checked("the clip is shorter than one tone window")
    frames = np.lib.stride_tricks.sliding_window_view(samples, window)[::hop].astype(np.float64)
    frame_db = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-12)
    quiet = frame_db <= speech_db - QUIET_BELOW_SPEECH_DB
    if np.count_nonzero(quiet) < MIN_QUIET_FRAMES:
        return _not_checked("not enough quieter time to look for steady tones")
    freqs, _times, spectrum = stft(samples, fs=rate, window="hann", nperseg=window, noverlap=window - hop,
                                   boundary=None, padded=False)
    power_db = 10 * np.log10(np.abs(spectrum[:, quiet]) ** 2 + 1e-12)
    mean_db = power_db.mean(axis=1)
    prominence = mean_db - median_filter(mean_db, size=TONE_NEIGHBOURHOOD_BINS, mode="nearest")
    tonal = ((prominence >= TONE_PROMINENCE_DB) & (power_db.std(axis=1) <= TONE_STABILITY_DB)
             & (freqs >= 30))
    tones = int(np.count_nonzero(tonal & ~np.concatenate(([False], tonal[:-1]))))
    check = {"status": "pass", "steady_tones": tones}
    if tones:
        hz = float(freqs[int(np.argmax(np.where(tonal, prominence, -np.inf)))])
        check.update(status="warn", strongest_hz=round(hz),
                     message=f"Steady tone near {hz:.0f} Hz in the quieter parts (hum, whine or background music).")
    return check


def _speakers(samples, rate, active, frame, speakers):
    if speakers is None:
        return _not_checked("the voice-match encoder is not installed")
    voiced = samples[_sample_mask(active, frame, len(samples))]
    blocks = min(MAX_BLOCKS, int(len(voiced) // (BLOCK_SECONDS * rate)))
    if blocks < 2:
        return _not_checked(f"needs at least {2 * BLOCK_SECONDS:g} s of speech")
    parts = np.array_split(voiced, blocks)
    try:
        results = []
        for index, part in enumerate(parts):
            rest = np.concatenate([other for position, other in enumerate(parts) if position != index])
            score = float(np.dot(speakers.embedding_from_samples(part, rate),
                                 speakers.embedding_from_samples(rest, rate)))
            results.append({"part": index + 1, "score": round(score, 4), "band": speakers.band(score)})
    except Exception as exc:  # advisory only: a failing encoder must not block a save
        return _not_checked(f"the speaker check did not run: {exc}")
    check = {"status": "pass", "parts": results}
    flagged = [result for result in results if result["band"] == "likely_different"]
    if flagged:
        worst = min(flagged, key=lambda result: result["score"])
        check.update(status="warn",
                     message=(f"Voice: part {worst['part']} scores {worst['score']:.2f} against the rest, "
                              "which the encoder reads as a different voice."))
    return check


def _summary(checks):
    penalty, warnings = 0, []
    for name, check in checks.items():
        if check["status"] != "warn":
            continue
        penalty += PENALTIES[name]
        if name == "clipping" and check["heavy"]:
            penalty += HEAVY_CLIPPING_PENALTY - PENALTIES["clipping"]
        warnings.append(check["message"])
    score = max(0, 100 - penalty)
    verdict = "good" if not warnings else ("review" if score >= 60 else "poor")
    return {"score": score, "verdict": verdict, "warnings": warnings, "checks": checks, "method": METHOD}


def assess(samples, rate, *, speakers=None):
    """Score one mono reference clip.

    ``speakers`` is speaker_similarity, or any object with ``embedding_from_samples(samples, rate)`` and
    ``band(score)``. Pass None when the proxy encoder is not installed; the speaker check then reports itself
    as not checked.
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    rate = int(rate)
    checks = {"clipping": _clipping(samples)}
    if len(samples) < MIN_SECONDS * rate:
        checks.update({name: _not_checked(f"the clip is shorter than {MIN_SECONDS:g} second") for name in ACOUSTIC_CHECKS})
        return _summary(checks)
    floor_db = float(np.percentile(_window_db(samples, round(FLOOR_WINDOW_SECONDS * rate),
                                              round(LEVEL_FRAME_SECONDS * rate)), 10))
    frame = round(LEVEL_FRAME_SECONDS * rate)
    levels = _frame_db(samples, frame)
    active = levels > floor_db + ACTIVE_ABOVE_FLOOR_DB
    speech_db = None if not active.any() else float(10 * np.log10(np.mean(10 ** (levels[active] / 10))))
    checks["noise_floor"] = _noise_floor(floor_db, speech_db)
    checks["reverb"] = _reverb(levels, active, floor_db)
    checks["background_tones"] = _background_tones(samples, rate, speech_db)
    checks["speakers"] = _speakers(samples, rate, active, frame, speakers)
    return _summary(checks)
