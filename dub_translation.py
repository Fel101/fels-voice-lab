"""Language tags and original cue text used when publishing a dub."""

import re

LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,3}")


def locale(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    if not LANGUAGE.fullmatch(value):
        raise ValueError("Enter a language tag such as en, es or pt-br.")
    return value


def source_text(scene, line):
    if "dub_source_text" in line:
        return line["dub_source_text"].strip()
    original = line["cue"].get("source_text")
    if original and scene["timeline"]["format"] != "transcript":
        from subtitle_import import subtitle_speech

        return subtitle_speech(original, scene["timeline"]["format"])[0]
    if original:
        return original
    from timed_scene import source_track

    sources = set(line["cue"].get("source_line_ids", []))
    return "\n".join(row["text"] for row in source_track(scene) if row["line_id"] in sources)
