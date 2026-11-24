"""Fit a take to its slot: a bounded time-stretch with a quality flag.

A line's target duration is advisory until a take is fitted to it. Fitting stretches the take only within
MAX_RATIO of its own length, records the factor, and says so. A take that would need more than the bound is not
changed, and the status says it needs a re-render. The stretch uses librosa's phase vocoder (ISC licence).
librosa is optional: it is imported only when a take is stretched, so fit_status, which the exporter uses, runs
on the core install. A phase-vocoder stretch can smear consonants and shift timbre, so a stretched result is
flagged for listening.
"""

import numpy as np

DEFAULT_MAX_RATIO = 0.15  # at most 15 % shorter or longer than the take as rendered
WITHIN_SLOT_TOLERANCE = 0.01  # within 1 % of the slot, the take is accepted as it is
STATUS_WITHIN_SLOT = "within_slot"
STATUS_STRETCHED = "stretched"
STATUS_NEEDS_RERENDER = "needs_rerender"
STRETCH_NOTE = "Phase-vocoder stretch: listen for smeared consonants and a changed timbre before accepting it."


def _check_bound(max_ratio, target_seconds):
    if not 0 < max_ratio <= 0.5:
        raise ValueError("max_ratio must be above 0 and at most 0.5")
    if target_seconds is None or target_seconds <= 0:
        raise ValueError("the target duration must be a positive number of seconds")


def fit_status(duration_seconds, target_seconds, *, max_ratio=DEFAULT_MAX_RATIO):
    """Decide how a take of this length fits its slot, without changing any audio."""
    _check_bound(max_ratio, target_seconds)
    if duration_seconds <= 0:
        raise ValueError("the take has no audio")
    factor = duration_seconds / target_seconds  # above 1: the take is longer than its slot and must speed up
    deviation = abs(factor - 1)
    if deviation <= WITHIN_SLOT_TOLERANCE:
        status = STATUS_WITHIN_SLOT
    elif deviation <= max_ratio:
        status = STATUS_STRETCHED
    else:
        status = STATUS_NEEDS_RERENDER
    report = {"status": status, "target_seconds": round(float(target_seconds), 3),
              "rendered_seconds": round(float(duration_seconds), 3), "factor": round(float(factor), 4),
              "max_ratio": max_ratio}
    if status == STATUS_STRETCHED:
        report["note"] = STRETCH_NOTE
    return report


def fit_to_slot(audio, sample_rate, target_seconds, *, max_ratio=DEFAULT_MAX_RATIO):
    """Return {"audio", ...report}: the audio stretched to the slot when the change is within the bound, otherwise the
    original samples. The report carries the status, the factor, and the actual length after the stretch."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    report = fit_status(samples.size / sample_rate, target_seconds, max_ratio=max_ratio)
    if report["status"] == STATUS_STRETCHED:
        try:
            import librosa
        except ImportError as exc:
            raise RuntimeError("Stretching a take needs librosa, which is not part of the core install. "
                               "fit_status works without it.") from exc
        samples = librosa.effects.time_stretch(samples, rate=report["factor"]).astype(np.float32)
    report["actual_seconds"] = round(samples.size / sample_rate, 3)
    report["error_seconds"] = round(report["actual_seconds"] - float(target_seconds), 3)
    return {"audio": samples, **report}
