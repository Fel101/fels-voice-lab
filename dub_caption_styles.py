"""ASS captions built on the same reviewed cue clock as the existing sidecars."""

import re

from dub_translation import source_text

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,44,&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,50,50,36,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _stamp(count):
    hour, rest = divmod(count, 360_000)
    minute, rest = divmod(rest, 6_000)
    second, subsecond = divmod(rest, 100)
    return f"{hour}:{minute:02d}:{second:02d}.{subsecond:02d}"


def _escape(text):
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\n", r"\N")


def _tokens(text):
    return [re.sub(r"[^\w]", "", word.casefold()) for word in text.split()]


def _word_marks(text, length, words):
    tokens = text.split()
    if not tokens:
        return "", True
    use_words = bool(words) and _tokens(text) == _tokens(" ".join(row["word"] for row in words))
    if use_words:
        # Karaoke marks define the start of each following word on the cue clock.
        boundaries = [min(length, max(0, round(row["start"] * 100))) for row in words]
        boundaries = sorted(boundaries)
    else:
        boundaries = [round(i * length / len(tokens)) for i in range(len(tokens))]
    if length < len(tokens) or len(set(boundaries)) < len(tokens) or boundaries[-1] >= length:
        return _escape(text), True
    formatted = [r"{\k" + str(boundaries[0]) + "}" if boundaries[0] else ""]
    for word, first, last in zip(tokens, boundaries, boundaries[1:] + [length]):
        formatted.append(r"{\kf" + str(last - first) + "}" + _escape(word) + " ")
    return "".join(formatted).rstrip(), not use_words


def write(scene, rows, style, aligned=None):
    lines = {line["line_id"]: line for line in scene["lines"]}
    entries, estimated = [], []
    for row in rows:
        first = round(row["start_seconds"] * 100)
        last = max(first + 1, round(row["end_seconds"] * 100))
        text = row["text"]
        if style == "bilingual":
            original = source_text(scene, lines[row["line_id"]])
            if original and original != text:
                text += "\n" + original
        if style == "karaoke":
            text, approximation = _word_marks(text, last - first, (aligned or {}).get(row["line_id"]))
            if approximation:
                estimated.append(row["line_id"])
        else:
            text = _escape(text)
        entries.append(f"Dialogue: 0,{_stamp(first)},{_stamp(last)},Default,,0,0,0,,{text}")
    return HEADER + "\n".join(entries) + "\n", estimated


def generated_words(cast, preview):
    import word_timing

    selected = {}
    for row in preview["lines"]:
        selected.setdefault(row["line_id"], []).append(row)
    by_id = {}
    for settings in preview["timing"].get("adjustments", []):
        line_id = settings["line_id"]
        parts = selected.get(line_id, [])
        segments = [
            (part["duration_seconds"], (cast.get_take(part["take_id"]).get("word_timings") or {}).get("words"))
            for part in parts
        ]
        if segments and all(words for _, words in segments):
            speed = settings.get("applied_speed", 1)
            by_id[line_id] = [
                {**word, "start": word["start"] / speed, "end": word["end"] / speed}
                for word in word_timing.shifted_words(segments)
            ]
    return by_id
