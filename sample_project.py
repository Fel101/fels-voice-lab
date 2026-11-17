"""The sample scene: two characters with Kokoro's preset voices and a short scene written for the Lab, so a new Lab has
something to render and hear without typing a word (POST /api/sample). It is made only when asked, through the same script
import as the page, and POST /api/sample/remove takes back everything it made, which leaves the library as it was.

data/scenes/sample.json records what was made: the characters, their voices and the scene. It sits beside the scenes, so a
backup of the library carries it. Removing deletes the scene with its batches and built previews (SceneStore.delete_scene),
then retires and purges each character with its voices, any recording added to them and every take made with them, and
drops the purge's record (CastStore.forget_purge). A sample character that another scene uses is not removed until those
lines have another voice, and a batch of the sample that is rendering is cancelled first."""

import json
import os
from datetime import datetime, timezone

import engine_adapters
from lab_errors import NotInstalled

RECORD_NAME = "sample.json"
RECORD_SCHEMA = 1
SCENE_NAME = "Sample: The parcel at the lighthouse"
TAG = "sample"
REASON = "The sample scene was removed."
NOT_INSTALLED = "The sample scene speaks with Kokoro 82M, which is not installed. Install it in Models & features first."
KINDS = ("character", "identity", "reference", "take")
# Each character takes the model's sample voice in the same place (Adapter.sample_voices).
CHARACTERS = (
    {
        "speaker": "EDIE",
        "name": "Edie (sample)",
        "description": "Keeps the lighthouse. A sample character, made by the Lab with a preset voice of Kokoro 82M.",
    },
    {
        "speaker": "TOMAS",
        "name": "Tomas (sample)",
        "description": "Brings the post by boat. A sample character, made by the Lab with a preset voice of Kokoro 82M.",
    },
)
# Written for the Lab, in the plain-text script form the page imports: one speech per line, NAME: words.
SCRIPT = """EDIE: You're late. The kettle's been on since the tide turned.
TOMAS: The fog came in thick past the point. I followed your light the whole way.
EDIE: That's what it's for. Sit down before you drip on everything.
TOMAS: I brought the post. Three letters, and a parcel that rattles.
EDIE: A parcel? Nobody sends me parcels.
TOMAS: This one says fragile on every side. Twice on the bottom.
EDIE: Then open it slowly. And if it's another clock from my sister, don't say a word.
TOMAS: It's a clock.
EDIE: Of course it is. Put it with the others.
"""


def engine():
    """The model the sample speaks with, and its two voices: the adapter that names them (Kokoro 82M's)."""
    (found,) = [
        (engine_id, adapter.sample_voices) for engine_id, adapter in engine_adapters.ADAPTERS.items() if adapter.sample_voices
    ]
    return found


class Busy(RuntimeError):
    """The sample's batch is still making a line: the removal waits for it to stop."""

    status_code = 409
    error_code = "busy"


def record_path(scenes):
    return scenes.data_root / RECORD_NAME


def read_record(scenes):
    """What the sample made, or None when there is no sample (or its record cannot be read)."""
    try:
        record = json.loads(record_path(scenes).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA:
        return None
    if not all(isinstance(item, str) for item in record.get("character_ids", [None])):
        return None
    return record


def _write_record(scenes, record):
    path = record_path(scenes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def _scene(scenes, record):
    if not record.get("scene_id"):
        return None
    try:
        return scenes.get_scene(record["scene_id"])
    except (KeyError, ValueError, RuntimeError):
        return None


def summary(scenes, cast):
    """The sample as the page shows it: its scene and characters, or None when nothing it made is left."""
    record = read_record(scenes)
    if record is None:
        return None
    names = {character["id"]: character["name"] for character in cast.list_cast()}
    scene = _scene(scenes, record)
    characters = [{"id": item, "name": names[item]} for item in record["character_ids"] if item in names]
    if scene is None and not characters:
        return None
    return {
        "scene_id": scene["scene_id"] if scene else None,
        "scene_name": scene["name"] if scene else None,
        "characters": characters,
        "created_at": record.get("created_at"),
    }


def status(scenes, cast, kokoro_installed):
    """GET /api/sample: the sample if there is one, and whether one can be made here."""
    return {
        "sample": summary(scenes, cast),
        "can_make": bool(kokoro_installed),
        "why_not": None if kokoro_installed else NOT_INSTALLED,
    }


def open_sample(scenes, cast, kokoro_installed, add_voice):
    """Make the sample, unless it exists. Returns (made, the sample). add_voice(character_id, request) adds a voice as
    POST /api/characters/{character_id}/identities does."""
    existing = summary(scenes, cast)
    if existing and existing["scene_id"]:
        return False, existing
    if not kokoro_installed:
        raise NotInstalled(NOT_INSTALLED)
    if existing or read_record(scenes):
        remove(scenes, cast)  # half made, say by a crash: take that back and make it again
    record = {
        "schema": RECORD_SCHEMA,
        "made_by": "the Lab's sample scene (sample_project.py)",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "character_ids": [],
        "identity_ids": [],
        "scene_id": None,
    }
    engine_id, voices = engine()
    try:
        speakers = {}
        for item, voice in zip(CHARACTERS, voices):
            character = cast.create_character(item["name"], item["description"], [TAG])
            record["character_ids"].append(character["id"])
            _write_record(scenes, record)
            label = voice.split("_", 1)[-1].replace("_", " ").title()
            identity = add_voice(character["id"], {"engine_id": engine_id, "label": label, "voice": voice})
            identity = cast.update_identity(identity["id"], {"status": "approved"})
            record["identity_ids"].append(identity["id"])
            speakers[item["speaker"]] = {"identity_id": identity["id"]}
        _write_record(scenes, record)
        manifest, _report = scenes.script_manifest(SCRIPT, "text", speakers, name=SCENE_NAME)
        record["scene_id"] = scenes.create_scene(manifest)["scene_id"]
        _write_record(scenes, record)
    except Exception:
        try:
            remove(scenes, cast)
        except Exception:
            pass  # The record stays, so the next removal finishes it.
        raise
    return True, summary(scenes, cast)


def _used_elsewhere(scenes, record):
    """The other scenes with a line in a sample character's voice."""
    mine = set(record["character_ids"])
    return [
        scene["name"]
        for scene in scenes.list_scenes()
        if scene["scene_id"] != record.get("scene_id") and any(line.get("character_id") in mine for line in scene["lines"])
    ]


def _counts(cast, record):
    counts, copies = dict.fromkeys(KINDS, 0), []
    names = {character["id"] for character in cast.list_cast()}
    for character_id in record["character_ids"]:
        if character_id not in names:
            continue
        preview = cast.purge_preview("character", character_id, require_retired=False)
        for kind in KINDS:
            counts[kind] += preview["would_purge"].get(kind, 0)
        copies += preview["external_copies"]
    return counts, copies


def removal_preview(scenes, cast):
    """GET /api/sample/remove/preview: what removing the sample deletes, and whether something stops it. With no sample,
    nothing."""
    record = read_record(scenes) or {"character_ids": [], "scene_id": None}
    scene = _scene(scenes, record)
    counts, copies = _counts(cast, record)
    jobs = len(scenes.list_jobs(scene["scene_id"])) if scene else 0
    return {
        "scene": {"scene_id": scene["scene_id"], "name": scene["name"], "jobs": jobs} if scene else None,
        "would_remove": counts,
        "used_elsewhere": _used_elsewhere(scenes, record),
        "external_copies": copies,
    }


def remove(scenes, cast):
    """POST /api/sample/remove: delete everything the sample made. Repeating it after a failure finishes the work."""
    record = read_record(scenes)
    if record is None:
        raise KeyError("There is no sample scene.")
    others = _used_elsewhere(scenes, record)
    if others:
        raise ValueError(
            "A sample character speaks in another scene: " + ", ".join(others) + ". Give those lines another voice first."
        )
    scene = _scene(scenes, record)
    jobs = 0
    if scene:
        stopping = False
        for job in scenes.list_jobs(scene["scene_id"]):
            if job["status"] in ("queued", "paused", "running"):
                scenes.cancel(job["id"])
                stopping = stopping or job["status"] == "running"
        if stopping or any(job["status"] == "running" for job in scenes.list_jobs(scene["scene_id"])):
            raise Busy("The sample's batch is stopping after the line it is making. Remove the sample again in a moment.")
        jobs = scenes.delete_scene(scene["scene_id"])["jobs"]
        record["scene_id"] = None
        _write_record(scenes, record)
    removed, copies = {"scene": 1 if scene else 0, "jobs": jobs, **dict.fromkeys(KINDS, 0)}, []
    names = {character["id"] for character in cast.list_cast()}
    for character_id in list(record["character_ids"]):
        if character_id in names:
            cast.retire("character", character_id, reason=REASON)
            report = cast.purge("character", character_id, reason=REASON, confirm=True)
            cast.forget_purge(report["batch_id"])
            for kind in KINDS:
                removed[kind] += report["purged"].get(kind, 0)
            copies += report["external_copies"]
        record["character_ids"].remove(character_id)
        _write_record(scenes, record)
    record_path(scenes).unlink(missing_ok=True)
    return {"removed": removed, "external_copies": copies}
