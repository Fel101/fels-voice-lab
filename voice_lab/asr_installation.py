"""Installed files for the Lab's isolated speech recognition tools."""

import json
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
ENVIRONMENT = ".venv-asr"
REQUIREMENTS = "requirements/asr-faster-whisper.txt"


def checkpoints(root=LAB):
    return json.loads((Path(root) / "catalog" / "asr_checkpoints.json").read_text(encoding="utf-8"))


def model_directory(root, spec):
    return Path(root) / "hf-cache" / spec["folder"]


def runtime_python(root=LAB):
    return Path(root) / ENVIRONMENT / "Scripts" / "python.exe"


def _size_matches(path, expected):
    try:
        return path.is_file() and path.stat().st_size == expected
    except OSError:
        return False


def status(root=LAB):
    runtime = runtime_python(root).is_file()
    models = {}
    for size, spec in checkpoints(root).items():
        folder = model_directory(root, spec)
        models[size] = runtime and all(_size_matches(folder / item["path"], item["size"]) for item in spec["files"])
    return {"ready": all(models.values()), "models": models}
