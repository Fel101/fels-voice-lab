"""Read SubRip and WebVTT as editable, millisecond-based scene cues."""

import html
import re


FORMATS = ("srt", "vtt")
MAX_TIMELINE_MS = 30 * 60 * 1000
MAX_CUE_TEXT = 10_000
MAX_SECTION_CUES = 500
MAX_SOURCE_MS = 10_000 * 60 * 60 * 1000
_BLOCK_BREAK = re.compile(r"(\n(?:[ \t]*\n)+)")
_SRT_TIME = r"\d{2,4}:[0-5]\d:[0-5]\d,\d{3}"
_VTT_TIME = r"(?:\d{2,4}:)?[0-5]\d:[0-5]\d\.\d{3}"
_TIMINGS = {
    "srt": re.compile(rf"({_SRT_TIME})[ \t]+-->[ \t]+({_SRT_TIME})"),
    "vtt": re.compile(rf"({_VTT_TIME})[ \t]+-->[ \t]+({_VTT_TIME})(?:[ \t]+[^\n]*)?"),
}
_VOICE = re.compile(r"<v(?:\.[^\s>]+)*[ \t]+([^>\n]+)>", re.I)
_NAMED_SPEECH = re.compile(r"^([^\W\d_][\w'’.\- ]{0,59}):[ \t]+", re.UNICODE)
_CUE_MARKUP = re.compile(r"</?(?:[biu]|font|c|v|lang|ruby|rt)(?=[.\s>])[^>]*>|<(?:\d+:)?\d{2}:\d{2}\.\d{3}>", re.I)


def _milliseconds(timestamp):
    clock, millis = re.split(r"[,.]", timestamp)
    seconds = 0
    for component in clock.split(":"):
        seconds = seconds * 60 + int(component)
    return seconds * 1000 + int(millis)


def _parse_block(lines, fmt):
    identifier = ""
    if "-->" not in lines[0]:
        identifier = lines.pop(0).strip()
    if not lines or not (times := _TIMINGS[fmt].fullmatch(lines.pop(0).strip())):
        raise ValueError("Expected a valid subtitle start and end.")
    start, end = map(_milliseconds, times.groups())
    if start >= end:
        raise ValueError("Set the end after the start and read the subtitle file again.")
    if end - start > MAX_TIMELINE_MS:
        raise ValueError("Split this single cue into shorter cues of at most 30 minutes and read the file again.")
    source = "\n".join(lines).strip()
    if len(source) > MAX_CUE_TEXT:
        raise ValueError(f"Subtitle text exceeds {MAX_CUE_TEXT:,} characters.")
    spoken, speaker = subtitle_speech(source, fmt)
    cue = {
        "start_ms": start,
        "end_ms": end,
        "source_start_ms": start,
        "source_end_ms": end,
        "source_text": source,
        "source_id": identifier[:200],
        "speaker": speaker,
    }
    return spoken, cue


def subtitle_speech(source, fmt):
    """Read the speech and optional label from a source cue without displaying its markup."""
    voice = _VOICE.match(source) if fmt == "vtt" else None
    spoken = html.unescape(_CUE_MARKUP.sub("", source)).strip()
    name = _NAMED_SPEECH.match(spoken) if not voice else None
    speaker = html.unescape(voice[1]) if voice else name[1] if name else ""
    speaker = " ".join(speaker.split())
    if len(speaker) > 60:
        raise ValueError("The cue's speaker label exceeds 60 characters.")
    if name:
        spoken = spoken[name.end() :]
    spoken = " ".join(spoken.split())
    if not spoken:
        raise ValueError("The cue has no text to speak.")
    return spoken, speaker


def parse_subtitles(text, fmt):
    """Return the cue rows and malformed blocks, with their original line numbers."""
    rows, unread = [], []
    blocks = _BLOCK_BREAK.split(text)
    number = 1
    if fmt == "vtt":
        header = blocks[0]
        if not re.fullmatch(r"WEBVTT(?:[ \t]+[^\n]*)?", header.split("\n", 1)[0]):
            raise ValueError("A WebVTT file must start with WEBVTT.")
        if "-->" in header:
            raise ValueError("Separate the WEBVTT header from the first subtitle cue with a blank line.")
    for index in range(0, len(blocks), 2):
        block = blocks[index]
        lines = block.split("\n")
        first = lines[0].strip()
        metadata = fmt == "vtt" and (
            index == 0 or first in {"STYLE", "REGION", "NOTE"} or first.startswith(("NOTE ", "NOTE\t"))
        )
        if first and not metadata:
            try:
                spoken, cue = _parse_block(lines, fmt)
                rows.append({"number": number, "text": spoken, "cue": cue})
            except ValueError as exc:
                unread.append({"number": number, "text": first[:200], "reason": str(exc)})
        if index + 1 < len(blocks):
            number += block.count("\n") + blocks[index + 1].count("\n")
    return rows, unread


def cue_overlaps(rows):
    """Report at most one earlier overlapping range per cue."""
    latest_end, latest_label = -1, ""
    warnings = []
    for start, end, label in sorted(rows, key=lambda row: row[0]):
        if start < latest_end:
            warnings.append({"code": "overlap", "message": f"{label} overlaps {latest_label}."})
        if end > latest_end:
            latest_end, latest_label = end, label
    return warnings


def clock_time(milliseconds):
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    prefix = f"{hours:02d}:" if hours else ""
    return f"{prefix}{minutes:02d}:{seconds:02d}.{fraction:03d}"


def scene_sections(rows):
    """Return source-clock offsets and row indexes for scenes small enough to render and edit."""
    if not rows:
        return []
    if len(rows) <= MAX_SECTION_CUES and max(row["cue"]["end_ms"] for row in rows) <= MAX_TIMELINE_MS:
        return [(0, list(range(len(rows))))]
    sections = []
    for index in sorted(range(len(rows)), key=lambda item: rows[item]["cue"]["start_ms"]):
        cue = rows[index]["cue"]
        if not sections or len(sections[-1][1]) == MAX_SECTION_CUES or cue["end_ms"] - sections[-1][0] > MAX_TIMELINE_MS:
            sections.append((cue["start_ms"], []))
        sections[-1][1].append(index)
    return sections
