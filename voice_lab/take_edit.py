"""Editing a take: trim, silence trim, fades and gain, applied to a copy of its samples.

A take is never changed. An edit reads the parent take's 16-bit samples, applies four steps in this fixed order, and the Cast
store saves the result as a new take that names its parent:

1. trim to start_seconds..end_seconds;
2. auto-trim: cut the leading and trailing parts of that range that stay under silence_threshold_db, keeping keep_silence_ms
   of each (silence inside the speech is never touched);
3. fade in and fade out on a raised-cosine curve (nothing is added; the ends are turned down);
4. gain_db, refused when it would push the loudest sample past full scale.

A trim alone copies the samples exactly. A fade or a gain recomputes them and rounds back to 16 bits. The parent's word and
phoneme times move with the trim (derive_word_record, derive_phoneme_record); fades and gain change no times.
"""
import math

import numpy as np

FULL_SCALE = 32768.0
MIN_RESULT_SECONDS = 0.05
MAX_FADE_MS = 5000.0
GAIN_DB_RANGE = (-60.0, 24.0)
THRESHOLD_DB_RANGE = (-90.0, -10.0)
KEEP_MS_RANGE = (0.0, 2000.0)
SUGGESTED_THRESHOLD_DB = -50.0
DEFAULT_KEEP_MS = 80.0
HOP_SECONDS, WINDOW_SECONDS = 0.010, 0.020      # loudness is read over 20 ms windows every 10 ms
MIN_KEPT_SECONDS = 0.04                         # a word or sound the trim leaves less than this of, and less than half of, is dropped
FIELDS = ("start_seconds", "end_seconds", "auto_trim", "silence_threshold_db", "keep_silence_ms",
          "fade_in_ms", "fade_out_ms", "gain_db")


def _number(request, name, low, high, default):
    value = request.get(name)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number.")
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}.")
    return round(float(value), 6)


def normalize_edit(request, duration_seconds):
    """The edit with every field present and typed, or ValueError saying what is wrong. A field it does not know is refused,
    so a misspelt one is not silently ignored; an edit that would change nothing is refused too."""
    if not isinstance(request, dict):
        raise ValueError("An edit must be a JSON object.")
    unknown = sorted(set(request) - set(FIELDS))
    if unknown:
        raise ValueError(f"Unknown edit field {unknown[0]!r}. An edit takes: {', '.join(FIELDS)}.")
    limit = float(duration_seconds)
    duration = round(limit, 6)
    auto_trim = request.get("auto_trim")
    if auto_trim is None:
        auto_trim = False
    if not isinstance(auto_trim, bool):
        raise ValueError("auto_trim must be true or false.")
    edit = {
        # the take's own length, to the last digit, is allowed: the bound is checked before the value is rounded
        "start_seconds": _number(request, "start_seconds", 0.0, limit, 0.0),
        "end_seconds": _number(request, "end_seconds", 0.0, limit, limit),
        "auto_trim": auto_trim,
        "silence_threshold_db": _number(request, "silence_threshold_db", *THRESHOLD_DB_RANGE, SUGGESTED_THRESHOLD_DB),
        "keep_silence_ms": _number(request, "keep_silence_ms", *KEEP_MS_RANGE, DEFAULT_KEEP_MS),
        "fade_in_ms": _number(request, "fade_in_ms", 0.0, MAX_FADE_MS, 0.0),
        "fade_out_ms": _number(request, "fade_out_ms", 0.0, MAX_FADE_MS, 0.0),
        "gain_db": _number(request, "gain_db", *GAIN_DB_RANGE, 0.0),
    }
    if edit["end_seconds"] <= edit["start_seconds"]:
        raise ValueError("end_seconds must be after start_seconds.")
    if (edit["start_seconds"] == 0.0 and edit["end_seconds"] == duration and not auto_trim and edit["fade_in_ms"] == 0.0
            and edit["fade_out_ms"] == 0.0 and edit["gain_db"] == 0.0):
        raise ValueError("This edit changes nothing. Set a trim, auto-trim, a fade or a gain.")
    return edit


def _dbfs(level):
    return None if level <= 0 else round(20.0 * math.log10(level / FULL_SCALE), 2)


def speech_bounds(samples, rate, threshold_db):
    """(first, last) sample of the part that is louder than threshold_db, read over 20 ms windows every 10 ms, or None when
    nothing is. The window that is loud decides, so the bounds are only as exact as the 10 ms hop."""
    count = len(samples)
    if not count:
        return None
    window = max(1, int(rate * WINDOW_SECONDS))
    hop = max(1, int(rate * HOP_SECONDS))
    squares = np.concatenate(([0.0], np.cumsum((samples.astype(np.float64) / FULL_SCALE) ** 2)))
    starts = np.arange(0, max(1, count - window + 1), hop)
    ends = np.minimum(starts + window, count)
    level = 10.0 * np.log10(np.maximum((squares[ends] - squares[starts]) / (ends - starts), 1e-20))
    loud = np.nonzero(level > threshold_db)[0]
    if not loud.size:
        return None
    return int(starts[loud[0]]), int(ends[loud[-1]])


def apply_edit(samples, rate, edit):
    """(edited samples, report) for a normalized edit. samples is a mono int16 array; it is not changed.

    The report names the part of the parent that was kept (start_sample, end_sample, start_seconds, end_seconds, on the
    parent's timeline) and what each step did, so the caller can move the parent's timings and record what happened."""
    if samples.ndim != 1:
        raise ValueError("Only a mono take can be edited.")
    total = len(samples)
    start = min(total, max(0, int(round(edit["start_seconds"] * rate))))
    end = min(total, max(start, int(round(edit["end_seconds"] * rate))))
    removed = None
    if edit["auto_trim"]:
        bounds = speech_bounds(samples[start:end], rate, edit["silence_threshold_db"])
        if bounds is None:
            raise ValueError(f"Nothing in the chosen range is louder than {edit['silence_threshold_db']:g} dB, "
                             "so auto-trim would leave nothing. Raise the threshold or trim by hand.")
        keep = int(round(edit["keep_silence_ms"] * rate / 1000.0))
        first, last = max(0, bounds[0] - keep), min(end - start, bounds[1] + keep)
        removed = {"start_seconds": round(first / rate, 6), "end_seconds": round((end - start - last) / rate, 6)}
        start, end = start + first, start + last
    clip = samples[start:end]
    if len(clip) < MIN_RESULT_SECONDS * rate:
        raise ValueError(f"The edited take would be shorter than {MIN_RESULT_SECONDS:g} seconds.")
    fade_in, fade_out = (int(round(edit[name] * rate / 1000.0)) for name in ("fade_in_ms", "fade_out_ms"))
    if fade_in + fade_out > len(clip):
        raise ValueError("The two fades are longer than the edited take.")
    gain = edit["gain_db"]
    if fade_in or fade_out or gain:
        data = clip.astype(np.float64)
        if fade_in:
            data[:fade_in] *= 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_in) / fade_in))
        if fade_out:
            data[len(data) - fade_out:] *= 0.5 * (1.0 + np.cos(np.pi * (np.arange(fade_out) + 1) / fade_out))
        peak = float(np.max(np.abs(data))) if len(data) else 0.0
        if gain:
            if peak * 10.0 ** (gain / 20.0) > FULL_SCALE - 1:
                room = 20.0 * math.log10((FULL_SCALE - 1) / peak)
                raise ValueError(f"A gain of {gain:g} dB would clip the loudest sample. This take can take at most "
                                 f"{math.floor(room * 10) / 10:g} dB.")
            data *= 10.0 ** (gain / 20.0)
        result = np.rint(data).astype(np.int16)
    else:
        result = clip.copy()
    report = {"start_sample": start, "end_sample": end, "start_seconds": round(start / rate, 6),
              "end_seconds": round(end / rate, 6), "duration_seconds": round(len(result) / rate, 6),
              "auto_trim_removed": removed, "fade_in_samples": fade_in, "fade_out_samples": fade_out,
              "peak_dbfs": _dbfs(float(np.max(np.abs(result.astype(np.int32)))) if len(result) else 0.0)}
    return result, report


def waveform(samples, rate, points):
    """What a page needs to draw a take and to say where auto-trim would cut: min and max per slice, the overall peak, and
    the part louder than the suggested threshold."""
    count = len(samples)
    points = max(1, min(int(points), count)) if count else 1
    if count:
        edges = np.linspace(0, count, points + 1).astype(np.int64)
        low = np.minimum.reduceat(samples, edges[:-1])
        high = np.maximum.reduceat(samples, edges[:-1])
        peaks = [[round(float(a) / FULL_SCALE, 4), round(float(b) / FULL_SCALE, 4)] for a, b in zip(low, high)]
        rms = math.sqrt(float(np.mean((samples.astype(np.float64) / FULL_SCALE) ** 2)))
        peak = float(np.max(np.abs(samples.astype(np.int32))))
    else:
        peaks, rms, peak = [[0.0, 0.0]], 0.0, 0.0
    bounds = speech_bounds(samples, rate, SUGGESTED_THRESHOLD_DB)
    return {"duration_seconds": round(count / rate, 6), "sample_rate": int(rate), "points": len(peaks), "peaks": peaks,
            "peak_dbfs": _dbfs(peak), "rms_dbfs": _dbfs(rms * FULL_SCALE),
            "speech": None if bounds is None else {
                "threshold_db": SUGGESTED_THRESHOLD_DB, "start_seconds": round(bounds[0] / rate, 3),
                "end_seconds": round(bounds[1] / rate, 3)}}


def _move(spans, start, end):
    """The spans (dicts with "start" and "end" in seconds) that survive a cut to start..end, on the cut clip's own timeline:
    moved earlier by start, and cut at 0 and at the end. A span the cut leaves less than MIN_KEPT_SECONDS of, and less than
    half of, is dropped. Returns (spans, how many were dropped, how many were shortened)."""
    kept_spans, dropped, shortened = [], 0, 0
    for span in spans:
        low, high = max(span["start"], start), min(span["end"], end)
        whole, kept = span["end"] - span["start"], high - low
        if kept < MIN_KEPT_SECONDS and kept < whole / 2:
            dropped += 1
            continue
        shortened += low > span["start"] or high < span["end"]
        kept_spans.append({**span, "start": round(low - start, 3), "end": round(high - start, 3)})
    return kept_spans, dropped, shortened


def _derived_from(parent_take_id, record, start, end, dropped, shortened):
    return {"take_id": parent_take_id, "audio_sha256": record.get("audio_sha256"), "start_seconds": round(start, 6),
            "end_seconds": round(end, 6), "spans_dropped": dropped, "spans_shortened": shortened}


def derive_word_record(record, parent_take_id, audio_sha256, start, end, duration):
    """The forced-alignment record of an edited take, from its parent's, or None when the parent has no usable one (the
    aligner can then be run on the new audio). Words move with the trim; the words the trim removes leave the record, and the
    text then follows the words that are left. Fades and gain change nothing."""
    if not record or record.get("error") or not record.get("words"):
        return None
    words, dropped, shortened = _move(record["words"], start, end)
    if not words:
        return None
    derived = {**record, "audio_sha256": audio_sha256, "duration_seconds": round(duration, 3), "words": words,
               "derived_from": _derived_from(parent_take_id, record, start, end, dropped, shortened)}
    if dropped:
        derived["text"] = " ".join(word["word"] for word in words)
    return derived


def derive_phoneme_record(record, parent_take_id, audio_sha256, start, end, duration):
    """The stored phoneme times (Kokoro's own) of an edited take, moved with the trim; None when the parent has none or the
    trim leaves no sound. The times are the model's frame times moved by the trim, so they are no longer whole frames."""
    if not record or not record.get("phonemes"):
        return None
    phonemes, dropped, shortened = _move(record["phonemes"], start, end)
    if not phonemes:
        return None
    words, words_dropped, words_shortened = _move(record.get("words", []), start, end)
    return {**record, "phonemes": phonemes, "words": words, "audio_sha256": audio_sha256,
            "duration_seconds": round(duration, 3),
            "derived_from": _derived_from(parent_take_id, record, start, end, dropped + words_dropped,
                                          shortened + words_shortened)}
