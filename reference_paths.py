"""Shared containment policy for saved studio reference audio."""

import json
import os
from pathlib import Path

import engine_adapters

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
    # A model with only voices of its own reads no voice recording but the bundled one.
    own_voices = not getattr(engine_adapters.get(key), "takes_voice_recordings", True)
    if not own_voices:
        roots.extend(((LAB / "data" / "voices").resolve(),
                      (LAB / "data" / "cast" / "media" / "references").resolve()))
        roots.extend(Path(root).expanduser().resolve() for root in
                     (configured_worker_roots() if extra_roots is None else extra_roots))
    if not reference.is_file() or not any(root in reference.parents for root in roots):
        allowed = ("voice-lab/reference/, voice-lab/data/voices/, or voice-lab/data/cast/media/references/"
                   if not own_voices else "voice-lab/reference/")
        raise ValueError(f"Reference audio must be an existing file under {allowed}.")
    adapter = engine_adapters.get(key)
    if adapter is not None:
        adapter.check_reference(reference)
    return reference
