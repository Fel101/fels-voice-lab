"""Shared containment policy for saved studio reference audio."""

import json
import os
from pathlib import Path

LAB = Path(__file__).resolve().parent
REFERENCE_ROOTS_ENV = "VOICE_LAB_REFERENCE_ROOTS_JSON"
CONVERSION_SOURCE_ROOTS_ENV = "VOICE_LAB_CONVERSION_SOURCE_ROOTS_JSON"


def configured_worker_roots(variable=REFERENCE_ROOTS_ENV):
    """Read the trusted allowlist supplied at child-process launch, never from a job."""
    try:
        values = json.loads(os.environ.get(variable, "[]"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Worker audio-root configuration must be a JSON list of absolute paths.") from exc
    if not isinstance(values, list) or any(not isinstance(value, str) or not Path(value).is_absolute() for value in values):
        raise ValueError("Worker audio-root configuration must be a JSON list of absolute paths.")
    return tuple(Path(value).resolve() for value in values)


def resolve_reference_path(key, path, *, extra_roots=None):
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        raise ValueError("Reference path must be a non-empty path.")
    reference = Path(path).expanduser().resolve()
    roots = [(LAB / "reference").resolve()]
    if key != "kokoro":
        roots.extend(((LAB / "data" / "voices").resolve(),
                      (LAB / "data" / "cast" / "media" / "references").resolve()))
        roots.extend(Path(root).expanduser().resolve() for root in
                     (configured_worker_roots() if extra_roots is None else extra_roots))
    if not reference.is_file() or not any(root in reference.parents for root in roots):
        allowed = ("voice-lab/reference/, voice-lab/data/voices/, or voice-lab/data/cast/media/references/"
                   if key != "kokoro" else "voice-lab/reference/")
        raise ValueError(f"Reference audio must be an existing file under {allowed}.")
    if key == "turbo":
        import soundfile as sf
        try:
            duration = sf.info(reference).duration
        except (RuntimeError, OSError) as exc:
            raise ValueError("Turbo reference must be readable audio.") from exc
        if duration <= 5.0:
            raise ValueError(f"Turbo reference must be longer than 5 seconds (received {duration:.3f}s). Select or record a longer reference clip.")
    return reference
