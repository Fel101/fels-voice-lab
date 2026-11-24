"""Read a script written as plain text (`NAME: words`) or in Fountain, for a new scene.

parse_script() turns the text into lines of speaker, words and acting direction, the list of speakers, and the lines it could
not read, each with its line number in the file. It creates nothing and knows nothing of Cast: scene_store.script_manifest()
gives each speaker a voice. Standard library only.

Plain text: one line per speech, `NAME: words`. A direction in brackets or parentheses at the start of the words, or after the
name (`NAME (quietly): words`), becomes the acting direction. A line with no name is narration: it is kept, with no speaker,
and only spoken when a narrator voice is chosen. A line that is only a direction, or a name with no words, is reported.

Fountain (https://fountain.io/syntax): a character cue (an upper-case line after a blank line, with dialogue under it, or a
line forced with @) and its dialogue become a line. A parenthetical before the words is their direction; one in the middle of a
speech starts a new line with that direction. Extensions such as (V.O.), (O.S.) and (CONT'D), and the dual-dialogue ^, are
taken off the name. Emphasis marks are taken out of the words. The title page's Title becomes the scene's name. Scene
headings, transitions, page breaks, sections, synopses, notes ([[...]]) and boneyard (/* ... */) are left out. Action is
kept as narration, like a plain-text line with no name.
"""

import json
import re

from voice_lab import subtitle_import

from voice_lab.voice_transcript import _unique, validate

TIMED_FORMATS = (*subtitle_import.FORMATS, "transcript")
FORMATS = ("text", "fountain", *TIMED_FORMATS)
MAX_NAME = 60
MAX_DIRECTION = 1000

_NAME = r"[^\W\d_][\w'’.\- ]{0,%d}" % (MAX_NAME - 1)
_TEXT_LINE = re.compile(r"(?P<name>" + _NAME + r"?)\s*(?:\((?P<inline>[^()]{1,200})\)\s*)?:(?P<rest>.*)")
_LEADING_DIRECTION = re.compile(
    r"\s*(?:\[(?P<square>[^\]]{1,%d})\]|\((?P<round>[^)]{1,%d})\))\s*(?P<rest>.*)" % (MAX_DIRECTION, MAX_DIRECTION), re.S
)
_WHOLE_DIRECTION = re.compile(r"\s*(?:\[[^\]]*\]|\([^)]*\))\s*")
_SCENE_HEADING = re.compile(r"(?:INT|EXT|EST|INT\.?/EXT|I/E)[ .]", re.I)
_TITLE_KEY = re.compile(r"(?P<key>[A-Za-z][A-Za-z ]{0,30}):(?P<value>.*)")
_EXTENSION = re.compile(r"\s*\([^()]*\)")


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def _emphasis(text):
    """Fountain's *, ** and _ marks taken out; a mark escaped with a backslash stays."""
    kept = {}

    def keep(match):
        kept[f"\0{len(kept)}\0"] = match.group(1)
        return f"\0{len(kept) - 1}\0"

    text = re.sub(r"\\([*_\\])", keep, text)
    text = text.replace("*", "").replace("_", "")
    for marker, character in kept.items():
        text = text.replace(marker, character)
    return text


def _blank_out(text, pattern):
    """Remove each match but keep its line breaks, so line numbers still point at the file."""
    return re.sub(pattern, lambda match: "\n" * match.group(0).count("\n"), text, flags=re.S)


class _Reader:
    def __init__(self):
        self.lines, self.unread, self.speakers, self.title = [], [], {}, ""

    def speech(self, number, speaker, text, direction=""):
        text, direction = _clean(text), _clean(direction)
        if not text:
            return
        if speaker is not None:
            key = speaker.casefold()
            entry = self.speakers.setdefault(key, {"name": speaker, "lines": 0})
            entry["lines"] += 1
            speaker = entry["name"]
        self.lines.append(
            {
                "number": number,
                "speaker": speaker,
                "kind": "dialogue" if speaker else "narration",
                "text": text,
                "direction": direction[:MAX_DIRECTION],
            }
        )

    def skip(self, number, text, reason):
        self.unread.append({"number": number, "text": _clean(text)[:200], "reason": reason})

    def result(self, fmt):
        return {
            "format": fmt,
            "title": self.title,
            "lines": self.lines,
            "speakers": list(self.speakers.values()),
            "narration": sum(line["speaker"] is None for line in self.lines),
            "unread": self.unread,
        }


def _speaker_line(line):
    """`NAME: words`, `NAME (direction): words` or `NAME: [direction] words` as (name, words, direction); None for a line
    that names nobody."""
    match = _TEXT_LINE.fullmatch(line)
    if not match or len(match.group("name").split()) > 4 or "//" in match.group("rest")[:2]:
        return None
    rest, direction = match.group("rest").strip(), match.group("inline") or ""
    leading = _LEADING_DIRECTION.fullmatch(rest)
    if leading:
        direction = " ".join(filter(None, (direction, leading.group("square") or leading.group("round"))))
        rest = leading.group("rest").strip()
    return _clean(match.group("name")), rest, _clean(direction)


def _plain(text):
    reader = _Reader()
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line:
            continue
        spoken = _speaker_line(line)
        before = None if spoken else _LEADING_DIRECTION.fullmatch(line)
        if before and (after := _speaker_line(before.group("rest").strip())):
            # `[softly] MIRA: Did you?`: a direction before the name is that line's direction, not words for a narrator.
            name, rest, direction = after
            spoken = name, rest, _clean(" ".join(filter(None, (before.group("square") or before.group("round"), direction))))
        if spoken:
            name, rest, direction = spoken
            if not rest:
                reader.skip(number, line, "a name with no words to say")
            else:
                reader.speech(number, name, rest, direction)
        elif _WHOLE_DIRECTION.fullmatch(line):
            reader.skip(number, line, "a direction with no words to say")
        else:
            reader.speech(number, None, line)
    return reader.result("text")


def _title_page(rows):
    """The title page's Title, and how many rows the title page takes (0 when there is none)."""
    if not rows or not _TITLE_KEY.fullmatch(rows[0].strip()) or _SCENE_HEADING.match(rows[0].strip()):
        return "", 0
    title, key, end = [], None, 0
    for index, row in enumerate(rows):
        if not row.strip():
            end = index + 1
            break
        match = _TITLE_KEY.fullmatch(row.strip()) if not row[:1].isspace() else None
        if match:
            key = match.group("key").strip().casefold()
            if key == "title" and match.group("value").strip():
                title.append(match.group("value").strip())
        elif key == "title":
            title.append(row.strip())
        end = index + 1
    return _clean(_emphasis(" ".join(title))), end


def _is_cue(row, previous_blank, next_row):
    stripped = row.strip()
    if stripped.startswith("@"):
        return bool(stripped[1:].strip()) and next_row is not None and bool(next_row.strip())
    if not previous_blank or next_row is None or not next_row.strip() or row[:1].isspace():
        return False
    name = _EXTENSION.sub("", stripped).rstrip("^").strip()
    return (
        bool(name)
        and len(name) <= MAX_NAME
        and name == name.upper()
        and any(char.isalpha() for char in name)
        and not _SCENE_HEADING.match(stripped)
        and not stripped.endswith("TO:")
        and not stripped.startswith(("!", ">", "~", ".", "#", "="))
    )


def _cue_name(row):
    name = row.strip().lstrip("@")
    name = _EXTENSION.sub("", name).rstrip("^").strip()
    return _clean(_emphasis(name))


def _fountain(text):
    reader = _Reader()
    text = _blank_out(text, r"/\*.*?\*/")
    text = _blank_out(text, r"\[\[.*?\]\]")
    rows = text.split("\n")
    reader.title, start = _title_page(rows)
    index, previous_blank = start, True
    while index < len(rows):
        row = rows[index]
        stripped = row.strip()
        number = index + 1
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        if not stripped:
            previous_blank = True
            index += 1
            continue
        if _is_cue(row, previous_blank, next_row):
            speaker = _cue_name(row)
            index += 1
            parts, direction, start_number = [], "", index + 1
            while index < len(rows) and (rows[index].strip() or rows[index] == "  "):
                piece = rows[index].strip()
                if piece.startswith("(") and piece.endswith(")"):
                    if parts:
                        reader.speech(start_number, speaker, _emphasis(" ".join(parts)), direction)
                        parts = []
                    direction, start_number = piece[1:-1], index + 2
                elif piece:
                    parts.append(piece.lstrip("~").strip())
                index += 1
            if parts:
                reader.speech(start_number, speaker, _emphasis(" ".join(parts)), direction)
            else:
                reader.skip(number, row, "a character name with no words to say")
            previous_blank = False
            continue
        skipped = (
            stripped.startswith(("#", "="))
            or (stripped.startswith(".") and not stripped.startswith(".."))
            or (stripped.startswith(">") and not stripped.endswith("<"))
            or (previous_blank and _SCENE_HEADING.match(stripped))
            or (
                previous_blank
                and stripped == stripped.upper()
                and stripped.endswith("TO:")
                and (next_row is None or not next_row.strip())
            )
        )
        if skipped:
            previous_blank = False
            index += 1
            continue
        # Action: the rows up to the next blank row are one paragraph of narration.
        parts, first = [], number
        while index < len(rows) and rows[index].strip():
            piece = rows[index].strip()
            if piece.startswith("!"):
                piece = piece[1:]
            if piece.startswith(">") and piece.endswith("<"):
                piece = piece[1:-1]
            parts.append(piece.lstrip("~"))
            index += 1
        reader.speech(first, None, _emphasis(" ".join(parts)))
        previous_blank = False
    return reader.result("fountain")


def _transcript(text):
    try:
        document = validate(json.loads(text, object_pairs_hook=_unique))
    except (ValueError, RecursionError) as exc:
        raise ValueError("This Voice Slate file is damaged or from a newer version. Export it again from Voice Slate.") from exc
    duration = document["source_media"]["duration_ms"]
    if duration > subtitle_import.MAX_TIMELINE_MS or len(document["cues"]) > 500:
        raise ValueError("Export SRT from Voice Slate and import it as subtitles to split this transcript into scenes.")
    labels = {}
    used = set()
    for speaker in document["speakers"]:
        label = " ".join(speaker["label"].split())[:60]
        if label.casefold() in used:
            label = f"{label[:45]} ({len(labels) + 1})"
            while label.casefold() in used:
                label += "!"
        labels[speaker["id"]] = label
        used.add(label.casefold())
    reader = _Reader()
    for index, row in enumerate(document["cues"], 1):
        edit = row.get("edit", {})
        speaker = labels.get(row.get("speaker_id"))
        reader.speech(index, speaker, edit.get("text", row["source_text"]))
        reader.lines[-1]["cue"] = {
            "start_ms": edit.get("start_ms", row["start_ms"]),
            "end_ms": edit.get("end_ms", row["end_ms"]),
            "source_start_ms": row["start_ms"],
            "source_end_ms": row["end_ms"],
            "source_text": row["source_text"],
            "source_id": row["id"],
            "speaker": speaker or "",
        }
        reader.lines[-1]["cue_id"] = row["id"]
        reader.lines[-1]["source_language"] = row.get("language", document["language"])
    return {
        **reader.result("transcript"),
        "duration_ms": duration,
        "warnings": subtitle_import.cue_overlaps(
            (
                line["cue"]["start_ms"],
                line["cue"]["end_ms"],
                f"Line {index} ({subtitle_import.clock_time(line['cue']['start_ms'])})",
            )
            for index, line in enumerate(reader.lines, 1)
        ),
        "transcript_source": {
            "source_media": document["source_media"],
            "provenance": document["provenance"],
            "speakers": document["speakers"],
            "speaker_turns": document.get("speaker_turns", []),
            "words": {cue["id"]: cue["words"] for cue in document["cues"] if cue.get("words")},
            "cue_languages": {cue["id"]: cue.get("language", document["language"]) for cue in document["cues"]},
        },
    }


def parse_script(text, fmt):
    """The script's lines, speakers and unread lines; see the module's notes. Raises ValueError for an unknown format."""
    if fmt not in FORMATS:
        raise ValueError(f"A script's format must be one of: {', '.join(FORMATS)}.")
    if not isinstance(text, str):
        raise ValueError("A script must be text.")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if fmt == "transcript":
        return _transcript(text)
    if fmt in subtitle_import.FORMATS:
        reader = _Reader()
        rows, reader.unread = subtitle_import.parse_subtitles(text, fmt)
        for row in rows:
            cue = row["cue"]
            reader.speech(row["number"], cue["speaker"] or None, row["text"])
            reader.lines[-1]["cue"] = cue
        return {
            **reader.result(fmt),
            "duration_ms": max((row["cue"]["end_ms"] for row in rows), default=0),
            "sections": [
                {"start_ms": offset, "cues": len(indices)} for offset, indices in subtitle_import.scene_sections(rows)
            ],
            "warnings": subtitle_import.cue_overlaps(
                (
                    row["cue"]["start_ms"],
                    row["cue"]["end_ms"],
                    f"Line {index} ({subtitle_import.clock_time(row['cue']['start_ms'])})",
                )
                for index, row in enumerate(rows, 1)
            ),
        }
    text = text.replace("\t", "    ")
    return _plain(text) if fmt == "text" else _fountain(text)


_ONE_TO_NINE = r"one|two|three|four|five|six|seven|eight|nine"
_TEENS = r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_TENS = r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_NUMBER_WORD = rf"(?:(?:{_TENS})(?:[ -](?:{_ONE_TO_NINE}))?|{_TEENS}|{_ONE_TO_NINE})"
_ROMAN_NUMBER = r"(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
# A title may follow the number after a separator, or after a space when it reads as a title: a capital first and no
# full stop at the end. So "Chapter 1 The Harbor" is a heading, and the one-line paragraph "Part two of the plan was simple."
# stays a passage.
TEXT_HEADING = re.compile(
    rf"(?:chapter|part|book)[ \t]+(?:[0-9]+|{_NUMBER_WORD}|{_ROMAN_NUMBER})"
    r"(?:\s*[:.–—-]\s*.*|[ \t]+(?-i:[A-Z0-9\"'“‘(])(?!.*[.…][\"'”’)]*$).*)?",
    re.I,
)
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(\S.*?)\s*#*\s*$")


PROSE_ROLE = re.compile(r"^(?P<speaker>[A-Z][A-Z0-9 '&.-]{1,39}):\s+(?P<text>.+)$")


def prose_blocks(content, markdown=False):
    paragraphs = []
    buffer = []
    fence = None

    def flush():
        if buffer:
            paragraphs.append((False, " ".join(" ".join(buffer).split())))
            buffer.clear()

    lines = [line.strip() for line in content.splitlines()]
    for index, line in enumerate(lines):
        if markdown and line.startswith(("```", "~~~")):
            flush()
            marker = line[:3]
            if not fence or marker == fence:
                fence = None if fence else marker
            continue
        if fence:
            continue
        match = MARKDOWN_HEADING.fullmatch(line) if markdown else None
        standalone = (index == 0 or not lines[index - 1]) and (index + 1 == len(lines) or not lines[index + 1])
        if match or (standalone and len(line) <= 120 and TEXT_HEADING.fullmatch(line)):
            flush()
            paragraphs.append((True, match[1].strip() if match else line))
        elif not line or (markdown and re.fullmatch(r"[-*_]{3,}", line)):
            flush()
        elif PROSE_ROLE.fullmatch(line) or re.match(r"\[(?:voice\s*:|emotion\s*:|pause\s)", line, re.I):
            flush()
            buffer.append(line)
        else:
            buffer.append(line)
    flush()
    return paragraphs
