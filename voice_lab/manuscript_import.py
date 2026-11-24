"""Read local manuscripts into a chapter-and-speaker preview without creating any scenes."""

import hashlib
from pathlib import PurePosixPath
import re

from voice_lab.script_import import PROSE_ROLE, prose_blocks
from voice_lab.scene_store import MAX_LINE_TEXT, MAX_SCENE_LINES, segment_text

FORMATS = {".txt", ".md", ".markdown"}
SPLITS = {"sentence", "paragraph", "chapter"}
MAX_UPLOAD = 24 * 1024 * 1024
MAX_TEXT = 700_000
MAX_CHAPTERS = 160
MAX_LINES = 6_000
MAX_SPEAKERS = 40
MAX_SECTION_TEXT = 2_400
MARK = re.compile(r"\[(voice\s*:[^\]\r\n]*|emotion\s*:[^\]\r\n]*|pause\s+[^\]\r\n]*)\]", re.I)


def _plain(value):
    return " ".join(str(value).split())


def _strip_markdown(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?<!\w)(\*{1,2}|_{1,2}|`)(\S.*?\S|\S)\1(?!\w)", r"\2", text)
    return re.sub(r"^(?:[-*+]\s+|>\s*)", "", text)


def _pause(markup):
    match = re.fullmatch(r"pause\s+(\d+(?:\.\d{1,3})?)\s*(ms|s)", markup, re.I)
    if not match:
        raise ValueError("Use pause markers such as [pause 500ms] or [pause 0.5s].")
    value = round(float(match[1]) * (1000 if match[2].lower() == "s" else 1))
    if not 0 <= value <= 10_000:
        raise ValueError("A pause marker can be at most 10 seconds.")
    return value


def _chapters(blocks, split, markdown=False):
    chapters, cast = [], {}
    title, lines, speaker, direction, leading = "", [], "", "", 0

    def join_titles(first, second):
        combined = f"{first}: {second}" if first else second
        if len(combined) > 120:
            raise ValueError("Shorten the chapter headings to 120 characters together and preview the document again.")
        return combined

    def speech(text):
        nonlocal leading
        text = _plain(_strip_markdown(text) if markdown else text)
        if not text:
            return
        for part in segment_text(text, limit=MAX_SECTION_TEXT):
            # Chapter splitting joins adjacent text with identical delivery. Explicit pauses and cast changes still separate it.
            previous = lines[-1] if lines else None
            if (
                split == "chapter"
                and previous
                and not leading
                and not previous["pause_after_ms"]
                and previous["speaker"] == speaker
                and previous["direction"] == direction
                and len(previous["text"]) + len(part) < MAX_LINE_TEXT
            ):
                previous["text"] += " " + part
                continue
            lines.append(
                {
                    "text": part,
                    "speaker": speaker,
                    "direction": direction,
                    "pause_before_ms": leading,
                    "pause_after_ms": None if split == "paragraph" else 0,
                }
            )
            leading = 0

    for heading, text in blocks:
        if not text:
            continue
        if heading:
            if lines:
                chapters.append({"title": title or "Opening", "lines": lines})
                title = ""
            title, lines, speaker, direction, leading = join_titles(title, text), [], "", "", 0
            continue
        match = PROSE_ROLE.fullmatch(text)
        if match:
            name = match["speaker"]
            speaker = cast.setdefault(name.casefold(), name)
            direction, text = "", match["text"]
        offset = 0
        for marker in MARK.finditer(text):
            payload = marker[1]
            speech(text[offset : marker.start()])
            offset = marker.end()
            tag, _, value = payload.partition(":")
            if tag.strip().lower() == "voice":
                name = _plain(value)
                if len(name) > 60:
                    raise ValueError("Voice tags must be at most 60 characters.")
                speaker = cast.setdefault(name.casefold(), name) if name else ""
                direction = ""
            elif tag.strip().lower() == "emotion":
                direction = _plain(value)[:240]
            else:
                wait = _pause(payload)
                if lines:
                    lines[-1]["pause_after_ms"] = min(10000, (lines[-1]["pause_after_ms"] or 0) + wait)
                else:
                    leading = min(10000, leading + wait)
        tail = text[offset:]
        if split == "sentence":
            for part in re.split(r"(?<=[.!?。！？])\s+(?=[\w“‘\"'])", tail):
                speech(part)
        else:
            speech(tail)
    if lines:
        chapters.append({"title": title or "Opening", "lines": lines})
    elif title and chapters:
        chapters[-1]["title"] = join_titles(chapters[-1]["title"], title)
    if not chapters:
        raise ValueError("The document is empty. Add the words you want the voice to read.")
    if len(chapters) > MAX_CHAPTERS or sum(len(part["lines"]) for part in chapters) > MAX_LINES:
        raise ValueError("A book can have up to 160 chapters and 6,000 passages.")
    editable, split_count = [], 0
    for chapter in chapters:
        passages = chapter["lines"]
        count = (len(passages) + MAX_SCENE_LINES - 1) // MAX_SCENE_LINES
        if count > 1:
            split_count += 1
        for index, start in enumerate(range(0, len(passages), MAX_SCENE_LINES), 1):
            title = chapter["title"]
            if count > 1:
                suffix = f" ({index} of {count})"
                title = title[: 120 - len(suffix)].rstrip() + suffix
            editable.append({"title": title, "lines": passages[start : start + MAX_SCENE_LINES]})
    if len(editable) > MAX_CHAPTERS:
        raise ValueError("This book has more than 160 chapters or chapter sections. Split it into volumes.")
    chapters = editable
    voices = sorted({row["speaker"] for part in chapters for row in part["lines"] if row["speaker"]}, key=str.casefold)
    if len(voices) > MAX_SPEAKERS:
        raise ValueError("The manuscript contains more than 40 named voices.")
    return chapters, voices, split_count


def preview(data, filename, split="paragraph"):
    if not isinstance(filename, str) or any(ch in filename for ch in ("\\", "/", ":")) or len(filename) > 240:
        raise ValueError("Supply a manuscript file name, without a directory.")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in FORMATS or not data or len(data) > MAX_UPLOAD or split not in SPLITS:
        raise ValueError("Choose a TXT or Markdown document up to 24 MiB.")
    markdown = suffix in {".md", ".markdown"}
    try:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        content = data.decode(encoding)
    except UnicodeError as exc:
        raise ValueError("Save the document as UTF-8 text and try again.") from exc
    if "\0" in content or len(content) > MAX_TEXT:
        raise ValueError("Choose a text document up to 700,000 characters.")
    blocks = prose_blocks(content, markdown)
    chapters, voices, split_count = _chapters(blocks, split, markdown)
    return {
        "title": _plain(PurePosixPath(filename).stem)[:120],
        "chapters": chapters,
        "speakers": voices,
        "sha256": hashlib.sha256(data).hexdigest(),
        "filename": filename,
        "warnings": (
            ["Long chapters are split into numbered sections. Each section gets its own audio file and chapter mark on export."]
            if split_count
            else []
        ),
        "narration": any(not row["speaker"] for part in chapters for row in part["lines"]),
    }
