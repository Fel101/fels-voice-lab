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

import re

FORMATS = ("text", "fountain")
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


def _plain(text):
    reader = _Reader()
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line:
            continue
        match = _TEXT_LINE.fullmatch(line)
        if match and len(match.group("name").split()) <= 4 and "//" not in match.group("rest")[:2]:
            name = _clean(match.group("name"))
            rest, direction = match.group("rest").strip(), match.group("inline") or ""
            leading = _LEADING_DIRECTION.fullmatch(rest)
            if leading:
                direction = _clean(" ".join(filter(None, (direction, leading.group("square") or leading.group("round")))))
                rest = leading.group("rest").strip()
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


def parse_script(text, fmt):
    """The script's lines, speakers and unread lines; see the module's notes. Raises ValueError for an unknown format."""
    if fmt not in FORMATS:
        raise ValueError(f"A script's format must be one of: {', '.join(FORMATS)}.")
    if not isinstance(text, str):
        raise ValueError("A script must be text.")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return _plain(text) if fmt == "text" else _fountain(text)
