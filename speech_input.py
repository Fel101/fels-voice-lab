"""Loopback speech input and the versioned options shared by integrations."""

import email.policy
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from email.parser import BytesParser
from pathlib import Path

import asr_installation

MODEL = "faster-whisper-tiny"
TRANSLATION_MODEL = "faster-whisper-small"
ALIAS = "whisper-1"
MODELS = {
    MODEL: ("tiny", "VOICE_LAB_ASR_MODEL", "Local faster-whisper Tiny"),
    TRANSLATION_MODEL: ("small", "VOICE_LAB_ASR_TRANSLATION_MODEL", "Local faster-whisper Small"),
}
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")
LIMIT = 25 * 1024 * 1024
FORMATS = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".mp4": "mov",
    ".m4a": "mov",
    ".mpeg": "mp3",
    ".mpga": "mp3",
    ".aac": "aac",
    ".webm": "matroska",
    ".ogg": "ogg",
    ".flac": "flac",
}
SLOTS = threading.BoundedSemaphore(2)


class InputUnavailable(ValueError):
    status_code = 503


class InputCancelled(ValueError):
    status_code = 499


def audio_suffix(name):
    if not isinstance(name, str) or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise ValueError("Use a named audio file.")
    suffix = Path(name).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError("Choose WAV, MP3, MP4, M4A, AAC, Ogg, FLAC or WebM audio.")
    return suffix


def settings(values, *, translate=False):
    model = values.get("model", ALIAS)
    if not isinstance(model, str) or (model != ALIAS and model not in MODELS):
        raise ValueError(f"Choose {MODEL}, {TRANSLATION_MODEL} or the {ALIAS} compatibility alias.")
    lang = values.get("language") or None
    if lang is not None and (not isinstance(lang, str) or not re.fullmatch("[a-z]{2,3}", lang)):
        raise ValueError("Use a two- or three-letter language code.")
    prompt = values.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 1000:
        raise ValueError("Use a prompt of up to 1,000 characters.")
    fmt = values.get("response_format", "json")
    if fmt not in ("json", "text", "verbose_json", "srt", "vtt"):
        raise ValueError("Choose json, text, verbose_json, srt or vtt.")
    if translate and lang:
        raise ValueError("Speech translation detects the source language and returns English.")
    try:
        temperature = float(values.get("temperature", 0))
    except (ValueError, TypeError) as exc:
        raise ValueError("Temperature must be between zero and one.") from exc
    if not 0 <= temperature <= 1:
        raise ValueError("Temperature must be between zero and one.")
    stamps = values.get("timestamp_granularities[]", [])
    if (
        not isinstance(stamps, list)
        or any(item not in ("segment", "word") for item in stamps)
        or (stamps and fmt != "verbose_json")
    ):
        raise ValueError("Select segment or word timestamp granularity with verbose_json.")
    return {
        "task": "translate" if translate else "transcribe",
        "model": model,
        "language": lang,
        "prompt": prompt,
        "format": fmt,
        "temperature": temperature,
        "word_timestamps": "word" in stamps,
    }


def multipart(content_type, raw):
    if (
        not isinstance(content_type, str)
        or not content_type.lower().startswith("multipart/form-data;")
        or len(content_type) > 350
    ):
        raise ValueError("Send a multipart/form-data upload with a file and a model.")
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("Invalid multipart content type.")
    try:
        message = BytesParser(policy=email.policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid multipart request.") from exc
    if not message.is_multipart() or message.defects:
        raise ValueError("The multipart upload is incomplete.")
    fields, found = {}, None
    allowed = {"model", "language", "prompt", "response_format", "temperature", "timestamp_granularities[]", "stream"}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_content_disposition() != "form-data" or part.is_multipart() or part.defects:
            raise ValueError("Unexpected multipart form entry.")
        if part.get("Content-Transfer-Encoding", "binary").lower() not in ("binary", "8bit", "7bit"):
            raise ValueError("Send the audio file as binary form data.")
        field = part.get_payload(decode=True)
        if name == "file":
            if found is not None or not field:
                raise ValueError("Send exactly one non-empty audio file.")
            found = (audio_suffix(part.get_filename()), field)
        elif name in allowed and len(field) <= 4096 and part.get_filename() is None:
            try:
                value = field.decode("utf-8").strip()
            except UnicodeError as exc:
                raise ValueError("Use UTF-8 form fields.") from exc
            if name == "timestamp_granularities[]":
                values = fields.setdefault(name, [])
                if len(values) == 2:
                    raise ValueError("Too many timestamp granularities.")
                values.append(value)
            elif name not in fields:
                fields[name] = value
            else:
                raise ValueError(f"Repeated form field {name}.")
        else:
            raise ValueError("Unexpected audio form field.")
    if found is None or "model" not in fields:
        raise ValueError("Send a file and model name.")
    if fields.pop("stream", "false") != "false":
        raise ValueError("File transcription returns a completed result. Use live sessions for partial text.")
    return found[0], found[1], fields


def ready_files(path):
    return all((path / name).is_file() for name in MODEL_FILES) and any(
        (path / name).is_file() for name in ("vocabulary.txt", "vocabulary.json")
    )


def configuration(model_id=MODEL):
    size, variable, _name = MODELS[model_id]
    python = Path(os.environ.get("VOICE_LAB_ASR_PYTHON") or asr_installation.runtime_python())
    override = os.environ.get(variable)
    spec = asr_installation.checkpoints()[size]
    model = Path(override) if override else asr_installation.model_directory(asr_installation.LAB, spec)
    return python, model


def speech_available(model_id=MODEL):
    python, model = configuration(model_id)
    return python.is_file() and ready_files(model)


def available_models():
    return [(ident, details[2]) for ident, details in MODELS.items() if speech_available(ident)]


def select_model(options):
    requested = options.get("model", ALIAS)
    if requested != ALIAS:
        return requested
    return TRANSLATION_MODEL if options["task"] == "translate" else MODEL


def run(root, raw, suffix, options, *, cancelled=None):
    if not raw or len(raw) > LIMIT or suffix not in FORMATS:
        raise ValueError("Use a supported audio file up to 25 MiB.")
    model_id = select_model(options)
    python, model = configuration(model_id)
    if not python.is_file() or not ready_files(model):
        raise InputUnavailable("Open System & backups and choose Install speech recognition, then try again.")
    deadline = time.monotonic() + 240
    while not SLOTS.acquire(timeout=0.15):
        _check(deadline, cancelled)
    try:
        _check(deadline, cancelled)
        with tempfile.TemporaryDirectory(dir=root, prefix=".speech-input-") as scratch:
            audio = Path(scratch) / ("source" + suffix)
            audio.write_bytes(raw)
            result = _worker(python, model, audio, options, deadline, cancelled)
            result["model"] = model_id
            result["provider"] = "local-faster-whisper"
            return result
    finally:
        SLOTS.release()


def _check(deadline, cancelled):
    if cancelled is not None and cancelled.is_set():
        raise InputCancelled("The transcription was cancelled.")
    if time.monotonic() > deadline:
        raise InputUnavailable("The local speech recognition worker timed out.")


def _worker(python, model, audio, options, deadline, cancelled):
    args = [
        str(python),
        "-I",
        "-X",
        "utf8",
        str(asr_installation.LAB / "speech_input_worker.py"),
        str(model),
        str(audio),
        FORMATS[audio.suffix],
        json.dumps(options, ensure_ascii=True),
    ]
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        with subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as worker:
            try:
                while True:
                    _check(deadline, cancelled)
                    try:
                        out, error = worker.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                worker.kill()
                worker.communicate()
                raise
    except OSError as exc:
        raise InputUnavailable("The local speech recognition interpreter could not start.") from exc
    if worker.returncode:
        reason = error.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(reason[-1][:300] if reason else "The local recognizer could not read this recording.")
    return json.loads(out)


def response(result, fmt):
    if fmt == "json":
        return {"text": result["text"]}
    if fmt == "verbose_json":
        return {
            key: value for key, value in result.items() if key in {"task", "language", "duration", "text", "segments", "words"}
        }
    if fmt == "text":
        return result["text"], "text/plain; charset=utf-8"
    from tools.scene_export import format_timestamp

    blocks = ["WEBVTT\n"] if fmt == "vtt" else []
    for index, segment in enumerate(result["segments"], 1):
        sep = "." if fmt == "vtt" else ","
        times = format_timestamp(segment["start"], sep) + " --> " + format_timestamp(segment["end"], sep)
        words = segment["text"].strip()
        if fmt == "vtt":
            import html

            words = html.escape(words, quote=False)
        blocks.append(f"{index}\n{times}\n{words}")
    return "\n\n".join(blocks) + "\n", "text/vtt; charset=utf-8" if fmt == "vtt" else "text/plain; charset=utf-8"
