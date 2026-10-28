"""Shared checks preventing selected audio from drifting away from scene inputs."""

from performance_settings import effective_settings


def validate_selected_take(scene_id, line, segment, take, spoken_text=None):
    # Segmentation can normalize whitespace around protected phrases/punctuation.
    # Compare content here; each take must also match its exact segment text below.
    joined = "".join("".join(item["text"].split()) for item in line["segments"])
    if joined != "".join(line["text"].split()):
        raise ValueError("Scene segments are stale relative to the current line text.")
    expected_id = f"{scene_id[:12]}:{line['line_id']}"
    if len(line["segments"]) > 1:
        expected_id += f":{line['segments'].index(segment) + 1:03d}"
    expected = {
        "identity_id": line["identity_id"], "character_id": line["character_id"],
        "reference_version_id": line.get("reference_version_id"), "line_id": expected_id,
        "text": segment["text"], "direction": line.get("direction", "").strip(),
    }
    for key, value in expected.items():
        if take.get(key) != value:
            raise ValueError(f"Selected take {take['id']} is stale or belongs to a different scene line/segment ({key}).")
    expected_identity = line.get("voice_identity")
    recorded_identity = (take.get("model") or {}).get("voice_identity")
    if expected_identity and (recorded_identity is not None or expected_identity.get("identity_type") == "preset_voice"):
        if recorded_identity != expected_identity:
            raise ValueError(f"Selected take {take['id']} is stale (voice identity settings).")
    if take.get("status") == "rejected":
        raise ValueError(f"Selected take {take['id']} is rejected.")
    if str(take.get("language", "")).casefold() != line.get("language", "English").casefold():
        raise ValueError(f"Selected take {take['id']} is stale (language).")
    settings = dict(take.get("settings") or {})
    use_pronunciations = settings.pop("use_pronunciations", False)
    if use_pronunciations != line.get("use_pronunciations", False):
        raise ValueError(f"Selected take {take['id']} is stale (pronunciation settings).")
    if effective_settings(line["engine_id"], settings) != effective_settings(line["engine_id"], line.get("settings", {})):
        raise ValueError(f"Selected take {take['id']} is stale (generation settings).")
    expected_spoken = spoken_text if spoken_text is not None else segment.get("spoken_text", segment["text"])
    if take.get("spoken_text") != expected_spoken:
        raise ValueError(f"Selected take {take['id']} is stale (spoken text/pronunciation dictionary).")
