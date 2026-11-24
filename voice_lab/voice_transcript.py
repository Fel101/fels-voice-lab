"""Reference validator and subtitle writer for the Fel's Voice transcript contract."""

import argparse
import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA = "fels-voice-transcript-v1"
MAX_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")
TIMING = {"authored", "measured", "estimated"}


def _fields(value, required, label, optional=()):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f"{label} has missing fields {sorted(missing)} or unknown fields {sorted(unknown)}.")
    return value


def _text(value, label, pattern=None):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be nonblank text.")
    value.encode("utf-8")  # Reject lone surrogates decoded from JSON escapes.
    if pattern and not pattern.fullmatch(value):
        raise ValueError(f"{label} has invalid text.")
    return value


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label} is not a supported value.")


def _span(value, lower, upper, label):
    start, end = value.get("start_ms"), value.get("end_ms")
    if type(start) is not int or type(end) is not int or not lower <= start < end <= upper:
        raise ValueError(f"{label} must be an integer millisecond span within {lower}–{upper}.")
    return start, end


def _media(value, label):
    _fields(value, ("sha256", "duration_ms", "kind"), label)
    _text(value["sha256"], f"{label}.sha256", re.compile(r"[0-9a-f]{64}"))
    duration = value["duration_ms"]
    if type(duration) is not int or duration < 1:
        raise ValueError(f"{label}.duration_ms must be a positive integer.")
    _choice(value["kind"], {"audio", "video"}, f"{label}.kind")
    return duration


def _ordered(items, label):
    if not isinstance(items, list):
        raise ValueError(f"{label} must be an array.")
    previous = -1
    for index, item in enumerate(items):
        if not isinstance(item, dict) or type(item.get("start_ms")) is not int or item["start_ms"] < previous:
            raise ValueError(f"{label} must be ordered by start_ms.")
        previous = item["start_ms"]
        yield item, f"{label}[{index}]"


def _validate_edit(edit, duration, label):
    _fields(edit, (), label, ("text", "start_ms", "end_ms", "reviewed_at"))
    if "text" in edit:
        _text(edit["text"], f"{label}.text")
    has_time = "start_ms" in edit or "end_ms" in edit
    if has_time:
        _span(edit, 0, duration, label)
    if "text" not in edit and not has_time:
        raise ValueError(f"{label} must change text or timing.")
    if "reviewed_at" in edit:
        at = datetime.fromisoformat(_text(edit["reviewed_at"], f"{label}.reviewed_at"))
        if at.utcoffset() is None:
            raise ValueError(f"{label}.reviewed_at requires a UTC offset.")


def validate(document):
    _fields(
        document,
        ("schema", "source_media", "language", "provenance", "speakers", "cues"),
        "Transcript",
        ("working_media", "speaker_turns"),
    )
    _choice(document["schema"], {SCHEMA}, "Transcript schema")
    duration = _media(document["source_media"], "source_media")
    if "working_media" in document and _media(document["working_media"], "working_media") != duration:
        raise ValueError("Working media must share the source duration.")
    _text(document["language"], "language", LANGUAGE)
    provenance = _fields(
        document["provenance"],
        ("producer", "version", "method"),
        "provenance",
        ("model",),
    )
    _text(provenance["producer"], "provenance.producer")
    _text(provenance["version"], "provenance.version")
    _choice(
        provenance["method"],
        {"manual", "asr", "subtitle_import", "synthetic_fixture"},
        "provenance.method",
    )
    if "model" in provenance or provenance["method"] == "asr":
        model = _fields(provenance.get("model"), ("id", "revision"), "provenance.model")
        _text(model["id"], "provenance.model.id")
        _text(model["revision"], "provenance.model.revision")
    speakers = {}
    if not isinstance(document["speakers"], list):
        raise ValueError("speakers must be an array.")
    for speaker in document["speakers"]:
        _fields(speaker, ("id", "label"), "Speaker")
        key = _text(speaker["id"], "Speaker ID", IDENTIFIER)
        if key in speakers:
            raise ValueError(f"Duplicate speaker ID {key}.")
        speakers[key] = _text(speaker["label"], "Speaker label")

    def check_speaker(value, label):
        if not isinstance(value, str) or value not in speakers:
            raise ValueError(f"{label} has an unknown speaker_id.")

    cues = set()
    for cue, label in _ordered(document["cues"], "cues"):
        _fields(
            cue,
            ("id", "start_ms", "end_ms", "source_text", "timing"),
            label,
            ("speaker_id", "language", "words", "edit"),
        )
        key = _text(cue["id"], f"{label}.id", IDENTIFIER)
        if key in cues:
            raise ValueError(f"Duplicate cue ID {key}.")
        cues.add(key)
        start, end = _span(cue, 0, duration, label)
        _text(cue["source_text"], f"{label}.source_text")
        _choice(cue["timing"], TIMING, f"{label}.timing")
        if cue.get("speaker_id") is not None:
            check_speaker(cue["speaker_id"], label)
        if "language" in cue:
            _text(cue["language"], f"{label}.language", LANGUAGE)
        for word, word_label in _ordered(cue.get("words", []), f"{label}.words"):
            _fields(word, ("text", "start_ms", "end_ms", "timing"), word_label)
            _text(word["text"], f"{word_label}.text")
            _span(word, start, end, word_label)
            _choice(word["timing"], TIMING, f"{word_label}.timing")
        if "edit" in cue:
            _validate_edit(cue["edit"], duration, f"{label}.edit")
    if not cues:
        raise ValueError("A transcript must contain at least one cue.")
    for turn, label in _ordered(document.get("speaker_turns", []), "speaker_turns"):
        _fields(turn, ("speaker_id", "start_ms", "end_ms", "timing"), label)
        check_speaker(turn["speaker_id"], label)
        _span(turn, 0, duration, label)
        _choice(turn["timing"], TIMING, f"{label}.timing")
    return document


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def load(path, media=None):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Transcript exceeds 2 MiB.")
    transcript = validate(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique))
    if media is not None and digest(media) != transcript["source_media"]["sha256"]:
        raise ValueError("Source media failed its SHA-256 check.")
    return transcript


def _stamp(ms, separator):
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def subtitles(document, fmt, *, edited=False):
    """Generate SRT/VTT from the source or edited clock, keeping the JSON intact."""
    validate(document)
    if fmt not in ("srt", "vtt"):
        raise ValueError("Subtitle format must be srt or vtt.")
    labels = {item["id"]: item["label"] for item in document["speakers"]}
    rows = []
    for cue in document["cues"]:
        edit = cue.get("edit", {}) if edited else {}
        words = " ".join(edit.get("text", cue["source_text"]).split())
        label = " ".join(labels.get(cue.get("speaker_id"), "").split())
        if fmt == "vtt":
            words = html.escape(words, quote=False)
            if label:
                words = f"<v {html.escape(label, quote=False)}>{words}</v>"
        elif label:
            words = f"{label}: {words}"
        rows.append(
            (
                edit.get("start_ms", cue["start_ms"]),
                edit.get("end_ms", cue["end_ms"]),
                cue["id"],
                words,
            )
        )
    separator = "," if fmt == "srt" else "."
    blocks = ["WEBVTT"] if fmt == "vtt" else []
    for index, (start, end, cue_id, text) in enumerate(sorted(rows, key=lambda row: row[0]), 1):
        identifier = index if fmt == "srt" else cue_id
        blocks.append(f"{identifier}\n{_stamp(start, separator)} --> {_stamp(end, separator)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--media", type=Path, help="Verify a separately selected source file's SHA-256.")
    args = parser.parse_args(argv)
    try:
        value = load(args.transcript, args.media)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"{SCHEMA}: {len(value['cues'])} cues, {len(value['speakers'])} speakers.")


if __name__ == "__main__":
    main()
