"""Advisory waveform and transcript diagnostics for evaluation records."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf


WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
# A clip whose peak reaches this, 0.01 dB below full scale, is flagged as clipping.
CLIPPING_PEAK = 10 ** (-0.01 / 20.0)
# The peak a model's clip is brought down to when it reaches CLIPPING_PEAK (with_headroom).
HEADROOM_PEAK = 10 ** (-1.0 / 20.0)


def with_headroom(samples):
    """A model's clip, scaled down to a -1 dBFS peak when its peak reaches full scale, and otherwise as it is.

    Written as 16-bit, a sample at or past full scale is cut off, and the take checks below flag the clip as clipping. A
    model can overshoot by a sample or two (Chatterbox 500M's smoke clip did), so every clip a model makes goes through this
    before anything writes it (worker_pool.speak). Quieter clips are left exactly as they are."""
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < CLIPPING_PEAK:
        return samples
    return (samples * (HEADROOM_PEAK / peak)).astype(samples.dtype, copy=False)


def normalize_words(text):
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("’", "'")
    return [word.replace("'", "") for word in WORD_RE.findall(normalized)]


def _edit_alignment(reference, hypothesis):
    rows, cols = len(reference) + 1, len(hypothesis) + 1
    cost = [[0] * cols for _ in range(rows)]
    back = [[None] * cols for _ in range(rows)]
    for i in range(1, rows):
        cost[i][0], back[i][0] = i, "delete"
    for j in range(1, cols):
        cost[0][j], back[0][j] = j, "insert"
    for i in range(1, rows):
        for j in range(1, cols):
            if reference[i - 1] == hypothesis[j - 1]:
                cost[i][j], back[i][j] = cost[i - 1][j - 1], "match"
                continue
            choices = ((cost[i - 1][j] + 1, "delete"),
                       (cost[i][j - 1] + 1, "insert"),
                       (cost[i - 1][j - 1] + 1, "substitute"))
            cost[i][j], back[i][j] = min(choices, key=lambda item: item[0])
    i, j = len(reference), len(hypothesis)
    ops = []
    while i or j:
        action = back[i][j]
        if action == "match":
            i -= 1; j -= 1
        elif action == "delete":
            ops.append(("deletion", reference[i - 1], None)); i -= 1
        elif action == "insert":
            ops.append(("insertion", None, hypothesis[j - 1])); j -= 1
        else:
            ops.append(("substitution", reference[i - 1], hypothesis[j - 1])); i -= 1; j -= 1
    return cost[-1][-1], list(reversed(ops))


def _repeated_phrases(words, minimum=2, maximum=6):
    found = []
    for width in range(minimum, min(maximum, len(words) // 2) + 1):
        for start in range(0, len(words) - width * 2 + 1):
            phrase = words[start:start + width]
            if phrase == words[start + width:start + width * 2]:
                value = " ".join(phrase)
                if value not in found:
                    found.append(value)
    return found


def compare_transcript(expected_text, heard_text, evaluator):
    """Compare expected text with a supplied transcript; never performs or implies ASR itself."""
    if heard_text is None or not str(heard_text).strip():
        return {"status": "not_transcribed", "evaluator": None, "wer": None, "cer": None,
                "operations": None, "flags": []}
    if not isinstance(evaluator, dict) or not str(evaluator.get("name", "")).strip() or not str(evaluator.get("version", "")).strip():
        raise ValueError("A transcript requires evaluator name and version (use an explicit manual evaluator when applicable).")
    expected = normalize_words(expected_text)
    heard = normalize_words(heard_text)
    if not expected:
        raise ValueError("Expected text must contain at least one word.")
    word_edits, operations = _edit_alignment(expected, heard)
    expected_chars = "".join(expected)
    heard_chars = "".join(heard)
    char_edits, _ = _edit_alignment(list(expected_chars), list(heard_chars))
    deletions = [left for op, left, _right in operations if op == "deletion"]
    flags = []
    if operations:
        flags.append("transcript_diff")
    if deletions:
        flags.append("missing_words")
    if len(heard) < len(expected) and heard == expected[:len(heard)]:
        flags.append("possible_truncation")
    repeated = _repeated_phrases(heard)
    if repeated:
        flags.append("repetition")
    return {"status": "compared", "evaluator": {"name": str(evaluator["name"]).strip(),
            "version": str(evaluator["version"]).strip()},
            "wer": word_edits / len(expected), "cer": char_edits / max(1, len(expected_chars)),
            "word_edits": word_edits, "expected_word_count": len(expected),
            "heard_word_count": len(heard), "operations": [
                {"type": op, "expected": left, "heard": right} for op, left, right in operations],
            "missing_words": deletions, "repeated_phrases": repeated, "flags": flags}


def _longest_internal_quiet_frames(quiet):
    """Longest run of quiet frames that neither starts at the first frame nor reaches the last."""
    longest, run_start = 0, None
    for index, is_quiet in enumerate(quiet + [False]):
        if is_quiet and run_start is None:
            run_start = index
        elif not is_quiet and run_start is not None:
            if run_start > 0 and index < len(quiet):
                longest = max(longest, index - run_start)
            run_start = None
    return longest


def analyze_audio(path, *, expected_text="", heard_text=None, evaluator=None):
    """Measure finite/rms/peak/silence properties and attach a supplied transcript comparison."""
    path = Path(path)
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    flattened = audio.reshape(-1)
    if not flattened.size or rate <= 0:
        raise ValueError("Audio file is empty or has an invalid sample rate.")
    finite = bool(np.isfinite(flattened).all())
    if not finite:
        raise ValueError("Audio contains non-finite samples.")
    peak = float(np.max(np.abs(flattened)))
    rms = float(np.sqrt(np.mean(np.square(flattened, dtype=np.float64))))
    duration = len(audio) / rate
    frame = max(1, int(rate * 0.1))
    levels = []
    for start in range(0, len(audio), frame):
        part = audio[start:start + frame].astype(np.float64)
        levels.append(float(np.sqrt(np.mean(np.square(part)))))
    silence_threshold = 10 ** (-60.0 / 20.0)
    quiet = [level < silence_threshold for level in levels]
    silent_duration = sum(quiet) * frame / rate
    leading = 0
    while leading < len(quiet) and quiet[leading]:
        leading += 1
    trailing = 0
    while trailing < len(quiet) and quiet[len(quiet) - trailing - 1]:
        trailing += 1
    flags = []
    if peak <= 10 ** (-90.0 / 20.0):
        flags.append("silent_audio")
    if peak >= CLIPPING_PEAK:
        flags.append("clipping")
    if silent_duration >= 1.0 and "silent_audio" not in flags:
        flags.append("long_silence")
    if expected_text:
        words = normalize_words(expected_text)
        if words and duration < max(0.35, 0.065 * len(words)):
            flags.append("duration_suspiciously_short")
    transcript = compare_transcript(expected_text, heard_text, evaluator) if heard_text is not None else {
        "status": "not_transcribed", "evaluator": None, "wer": None, "cer": None,
        "operations": None, "flags": []}
    flags.extend(flag for flag in transcript.get("flags", []) if flag not in flags)
    return {"sample_rate": int(rate), "channels": int(audio.shape[1]), "sample_count": int(len(audio)),
            "duration_seconds": duration, "finite_samples": finite,
            "peak_dbfs": 20 * np.log10(peak) if peak > 0 else None,
            "rms_dbfs": 20 * np.log10(rms) if rms > 0 else None,
            "silence_threshold_dbfs": -60.0, "quiet_frame_seconds": silent_duration,
            "leading_quiet_seconds": min(duration, leading * frame / rate),
            "trailing_quiet_seconds": min(duration, trailing * frame / rate),
            "longest_internal_quiet_seconds": min(duration, _longest_internal_quiet_frames(quiet) * frame / rate),
            "transcript": transcript, "flags": flags}


TAKE_CHECKER = "take-waveform-v1"
TAKE_LONG_PAUSE_SECONDS = 1.0


def take_checks(path, *, expected_text=""):
    """Advisory waveform checks recorded when a Cast take is saved.

    Silence, full-scale samples and too-short audio use the evaluation rules. A long pause
    counts only a quiet run inside the take, because leading and trailing quiet and ordinary
    sentence pauses are not defects. These checks never judge words, identity or acting.
    """
    report = analyze_audio(path, expected_text=expected_text)
    flags = [flag for flag in report["flags"] if flag != "long_silence"]
    pause = report["longest_internal_quiet_seconds"]
    if pause >= TAKE_LONG_PAUSE_SECONDS:
        flags.append("long_pause")
    return {"status": "checked", "checker": TAKE_CHECKER, "flags": flags, "longest_pause_seconds": round(pause, 3)}
