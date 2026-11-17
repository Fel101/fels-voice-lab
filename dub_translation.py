"""Language tags and original cue text used when publishing a dub."""

import json
import re

import connector_profiles
from scene_store import digest_json

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


def draft(folder, scene, line, request, profile):
    source = locale(request.get("source_language"))
    target = locale(request.get("target_language"))
    if source == target:
        raise ValueError("Choose a different target language for translation.")
    original = source_text(scene, line)
    if not original:
        raise ValueError("Enter and save the original wording before asking for a draft.")
    key = digest_json([profile["revision"], source, target, original])
    cache = folder / "translation-cache"
    path = cache / (key + ".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    words = connector_profiles.translate(profile, original, source, target)
    if len(words) > 10_000:
        raise ValueError("The draft is too long. Shorten the original wording and try again.")
    result = {"text": words, "source_text": original, "target_language": target, "provider_revision": profile["revision"]}
    from dubbing import write_json_atomic

    cache.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, result)
    return result
