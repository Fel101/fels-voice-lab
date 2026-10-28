"""Draft transcripts for saved Cast reference clips.

The suggestion runs asr_worker.py in the isolated .venv-asr environment, so the main lab
environment never imports faster-whisper. The model is a local copy under hf-cache/. Nothing
here downloads, saves, or approves anything: the caller shows the draft for a person to check.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

LAB = Path(__file__).resolve().parent
ASR_PYTHON = LAB / ".venv-asr" / "Scripts" / "python.exe"
ASR_WORKER = LAB / "asr_worker.py"
ASR_MODEL_DIR = LAB / "hf-cache" / "faster-whisper-tiny-d90ca5fe"
MODEL_LABEL = "faster-whisper tiny (Systran snapshot d90ca5fe)"
TIMEOUT_SECONDS = 180
_suggest_lock = threading.Lock()


class TranscriptUnavailable(RuntimeError):
    """The isolated environment or model copy is missing."""


class TranscriptBusy(RuntimeError):
    """Another suggestion is still running; the client should try again."""


def suggest(audio_path):
    audio = Path(audio_path)
    if not ASR_PYTHON.is_file() or not ASR_WORKER.is_file():
        raise TranscriptUnavailable(
            "Transcript suggestions are not set up. They need the optional .venv-asr environment described in docs/SETUP.md.")
    if not (ASR_MODEL_DIR / "model.bin").is_file():
        raise TranscriptUnavailable("The local transcript model is missing from hf-cache/. docs/SETUP.md says which copy it needs.")
    if not audio.is_file():
        raise FileNotFoundError("Reference audio is missing.")
    if not _suggest_lock.acquire(blocking=False):
        raise TranscriptBusy("Another transcript suggestion is running. Wait for it to finish, then try again.")
    try:
        completed = subprocess.run(
            [str(ASR_PYTHON), "-I", str(ASR_WORKER), "--model-dir", str(ASR_MODEL_DIR), "--audio", str(audio)],
            capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_SECONDS, check=False,
            cwd=str(LAB), env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Transcript suggestion took too long and was stopped.") from exc
    finally:
        _suggest_lock.release()
    if completed.returncode:
        detail = (completed.stderr or "").strip().splitlines()
        raise RuntimeError("Transcript suggestion failed: " + (detail[-1] if detail else f"exit {completed.returncode}"))
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Transcript suggestion produced no result.")
    result = json.loads(lines[-1])
    return {**result, "model": MODEL_LABEL, "draft": True}
