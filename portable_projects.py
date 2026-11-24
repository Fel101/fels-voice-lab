"""Portable local voice and project archives. Every optional asset has a declared role and SHA-256."""

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

import soundfile as sf

import book_export
import dubbing
import engine_adapters
from cast_store import MAX_REFERENCE_TEXT, REFERENCE_POLICIES, TRANSCRIPT_ENGINES
from model_facts import output_use
from reference_catalog import _choose_audition
import synthetic_label
from book_store import Books, metadata, milliseconds, pronunciations
from tools.scene_export import sha256_file
from worker_pool import read_catalog
from scene_store import canonical_json, digest_json, new_id, utc_now

SCHEMA = "fels-voice-portable-v1"
MAX_BYTES = 96 * 1024 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
MAX_FILES = 240
ID = re.compile(r"[a-f0-9]{32}")
ASSET = re.compile(r"asset-[0-9]{3}\.(?:wav|mp3|ogg|flac|m4a|mp4|mov|mkv|webm|jpg)")
SCENE_FIELDS = {"name", "timeline", "source_media", "transcript_source", "dub_target_language", "book_pronunciations"}
LINE_FIELDS = {
    "line_id",
    "line_type",
    "text",
    "language",
    "direction",
    "settings",
    "pause_before_ms",
    "pause_after_ms",
    "take_count",
    "target_duration_ms",
    "use_pronunciations",
    "cue",
    "dub",
    "dub_source_text",
    "manuscript_text",
    "manuscript_spoken",
    "manuscript_speaker",
    "manuscript_direction",
}


def _object(raw):
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError("The archive manifest has a repeated key.")
            result[key] = val
        return result

    def invalid_constant(token):
        raise ValueError(f"Invalid JSON number in the archive: {token}.")

    obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(obj, dict):
        raise ValueError("Expected a portable archive manifest object.")
    return obj


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid portable archive or project ID.")
    return value


@contextmanager
def checked(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Choose a portable archive up to 96 MiB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("This file could not be read as a ZIP archive.") from exc
    with archive:
        rows = archive.infolist()
        if not rows or len(rows) > MAX_FILES + 1 or sum(row.file_size for row in rows) > MAX_BYTES:
            raise ValueError("The archive exceeds the file-count or expanded-size limit.")
        names = [row.filename for row in rows]
        if (
            len(set(names)) != len(names)
            or "manifest.json" not in names
            or any(row.flag_bits & 1 or (row.filename != "manifest.json" and not ASSET.fullmatch(row.filename)) for row in rows)
        ):
            raise ValueError("The archive has an unexpected, duplicate or encrypted entry.")
        if archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
            raise ValueError("The portable manifest is too large.")
        try:
            envelope = _object(archive.read("manifest.json"))
            checksum = envelope.pop("checksum", None)
            if envelope.get("schema") != SCHEMA or envelope.get("kind") not in {"persona", "scene", "dub", "book"}:
                raise ValueError("Unsupported portable archive version or role.")
            if checksum != digest_json(envelope):
                raise ValueError("Portable manifest checksum failed.")
            if not isinstance(envelope.get("assets"), dict) or not isinstance(envelope.get("contents"), dict):
                raise ValueError("The portable archive is missing its assets or contents.")
            if set(envelope["assets"]) != set(names) - {"manifest.json"}:
                raise ValueError("Files in the archive do not match the manifest.")
            for name, receipt in envelope["assets"].items():
                if not isinstance(receipt, dict) or receipt.get("bytes") != archive.getinfo(name).file_size:
                    raise ValueError("A portable asset size did not match.")
                digest = hashlib.sha256()
                with archive.open(name) as source:
                    for chunk in iter(lambda: source.read(256 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != receipt.get("sha256"):
                    raise ValueError(f"Portable asset {name} failed its SHA-256.")
            yield envelope, archive
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, RecursionError) as exc:
            raise ValueError("Could not verify the portable archive.") from exc


class Files:
    def __init__(self):
        self.sources, self.assets = {}, {}
        self.total = 0

    def add(self, path, role, expected=None):
        path = Path(path)
        length = path.stat().st_size
        name = f"asset-{len(self.assets) + 1:03d}{path.suffix.lower()}"
        if len(self.assets) >= MAX_FILES or not ASSET.fullmatch(name) or self.total + length > MAX_BYTES - MAX_MANIFEST:
            raise ValueError("Choose fewer media assets or export this project without audio.")
        checksum = sha256_file(path)
        if expected is not None and expected != checksum:
            raise ValueError("A saved asset changed. Restore its original bytes before packaging.")
        self.assets[name] = {"role": role, "bytes": length, "sha256": checksum}
        self.sources[name] = path
        self.total += length
        return name

    def add_audio(self, raw, role):
        if len(self.assets) >= MAX_FILES or self.total + len(raw) > MAX_BYTES - MAX_MANIFEST:
            raise ValueError("Choose fewer recordings and export the file again.")
        name = f"asset-{len(self.assets) + 1:03d}.wav"
        self.assets[name] = {"role": role, "bytes": len(raw), "sha256": _sha(raw)}
        self.sources[name] = raw
        self.total += len(raw)
        return name

    def publish(self, storage, kind, data):
        ident = new_id()
        storage.mkdir(parents=True, exist_ok=True)
        envelope = {"schema": SCHEMA, "kind": kind, "contents": data, "assets": self.assets}
        envelope["checksum"] = digest_json(envelope)
        with tempfile.TemporaryDirectory(dir=storage, prefix=".portable-") as temporary:
            ready = Path(temporary) / f"{ident}.zip"
            with zipfile.ZipFile(ready, "w", compression=zipfile.ZIP_STORED) as package:
                package.writestr("manifest.json", canonical_json(envelope).encode())
                for name, content in self.sources.items():
                    if isinstance(content, bytes):
                        package.writestr(name, content)
                    else:
                        package.write(content, name)
            with checked(ready.read_bytes()):
                pass
            ready.replace(storage / ready.name)
        return {"id": ident, "kind": kind, "files": len(self.assets), "download": f"/api/portable/{ident}.zip"}


def _model_pin(engine_id, options):
    entry = next((row for row in read_catalog()["engines"] if row["id"] == engine_id), {})
    return {
        "name": entry.get("name", engine_id),
        "allowed_use": output_use(engine_adapters.get(engine_id))["text"],
        "checkpoint": options.get("checkpoint") or entry.get("checkpoint"),
        "revision": options.get("checkpoint_revision") or entry.get("local_revision"),
        "revision_basis": "Cast identity" if options.get("checkpoint_revision") else "Lab catalog",
    }


def _voice(cast, ident, files, audio):
    item = cast.get_generation_identity(ident) if audio else cast.get_identity(ident)
    entry = {
        "id": ident,
        "label": item["label"],
        "engine_id": item["engine_id"],
        "kind": item["kind"],
        "voice_options": item.get("engine_options", {}),
        "notes": item.get("notes", ""),
        "origin_status": item["status"],
        "licence": item["licence"],
    }
    entry["model"] = _model_pin(item["engine_id"], entry["voice_options"])
    if audio and item["kind"] == "reference":
        reference = item.get("reference")
        if item["status"] != "approved" or not reference or not reference.get("allowed_use") or not reference.get("origin"):
            raise ValueError("Only an approved reference with recorded origin and permitted use can be included.")
        entry["sample"] = {
            "source_type": "designed" if _is_design(reference) else "recording",
            "asset": files.add(reference["audio_path"], "voice_reference", reference["audio_sha256"]),
            "origin": reference["origin"],
            "allowed_use": reference["allowed_use"],
            "transcript": reference.get("transcript") or "",
            "source_id": reference["id"],
            "sha256": reference["audio_sha256"],
        }
    return entry


def _is_design(reference):
    # The Lab's saved voice-design workflow records this provenance with the source WAV.
    return reference["origin"].startswith("Original synthetic voice design via ")


def _include_audition(cast, identity, files):
    reference_id = identity.get("approved_reference_id")
    takes = [
        take
        for take in cast.list_takes(identity["id"])
        if not take["retired"] and take["status"] != "rejected" and take.get("reference_version_id") == reference_id
    ]
    take = _choose_audition(takes, reference_id)
    if not take:
        raise ValueError("Make a short take for this voice in Perform lines, then export the voice again.")
    path = Path(cast.get_media_path("take", take["id"]))
    if sha256_file(path) != take["audio_sha256"]:
        raise ValueError("The saved audition has changed. Make a new take in Perform lines and export again.")
    with sf.SoundFile(path) as audio:
        samples = audio.read(12 * audio.samplerate, dtype="int16")
        rate = audio.samplerate
    if not len(samples):
        raise ValueError("The saved audition is empty. Make a new take in Perform lines and export again.")
    if len(samples) / rate == take["duration_seconds"]:
        raw = path.read_bytes()
    else:
        raw = synthetic_label.wav_bytes(samples.astype("float32") / 32768, rate, engine_id=identity["engine_id"])
    return {
        "asset": files.add_audio(raw, "voice_audition"),
        "text": take["text"],
        "spoken_text": take["spoken_text"],
        "language": take["language"],
        "direction": take["direction"],
        "settings": take["settings"],
        "licence": take["licence"],
    }


def _confirmed(request, key):
    value = request.get(key, False)
    if type(value) is not bool:
        raise ValueError(f"{key} must be true or false.")
    return value


def export_persona(sc, request):
    identity = sc.cast.get_generation_identity(_id(request.get("identity_id")))
    if identity["status"] != "approved":
        raise ValueError("Approve this voice in Cast before exporting it.")
    reference = identity.get("reference")
    if reference and not _is_design(reference) and request.get("permission") is not True:
        raise ValueError("Confirm you have permission to share this voice and its approved recording.")
    files = Files()
    item = _voice(sc.cast, identity["id"], files, bool(reference))
    item["audition"] = _include_audition(sc.cast, identity, files)
    character = sc.cast.get_character(identity["character_id"])
    author = request.get("author", "")
    if not isinstance(author, str) or len(author.strip()) > 120:
        raise ValueError("Use an author name of up to 120 characters.")
    entry = {
        "voice": item,
        "author": author.strip(),
        "character": {key: character[key] for key in ("name", "description", "tags")},
        "pronunciations": [
            {key: row[key] for key in ("source", "replacement", "notes")}
            for row in sc.cast.list_pronunciations(character["id"])
            if row["character_id"] == character["id"]
        ],
    }
    return files.publish(sc.root / "out" / "portable", "persona", entry)


def _scene_document(scene):
    return {
        **{key: value for key, value in scene.items() if key in SCENE_FIELDS},
        "source_scene_id": scene["scene_id"],
        "lines": [
            {**{key: value for key, value in line.items() if key in LINE_FIELDS}, "voice_id": line["identity_id"]}
            for line in scene["lines"]
        ],
    }


def export_project(sc, request):
    kind, ident = request.get("kind"), _id(request.get("id"))
    if kind not in {"scene", "book", "dub"}:
        raise ValueError("Choose a scene, dub or book project.")
    media = _confirmed(request, "include_media")
    voices = _confirmed(request, "include_voices")
    if voices and request.get("permission") is not True:
        raise ValueError("Confirm permission to include approved voice references.")
    files = Files()
    book = None
    if kind == "book":
        book = Books(sc).get(ident)
        keys = book["chapters"]
    else:
        keys = [ident]
    scenes = [sc.get_scene(key) for key in keys]
    if kind == "dub" and not scenes[0].get("timeline"):
        raise ValueError("A dub project needs a timed scene.")
    identities = sorted({line["identity_id"] for scene in scenes for line in scene["lines"]})
    listed = [_voice(sc.cast, key, files, voices) for key in identities]
    content = {
        "id": ident,
        "title": book["metadata"]["title"] if book else scenes[0]["name"],
        "scenes": [_scene_document(scene) for scene in scenes],
        "voices": listed,
        "include_media": media,
        "include_voices": voices,
        "media": {},
    }
    if book:
        content["book"] = {key: book[key] for key in ("metadata", "gap_ms", "source", "pronunciations")}
        if media and book["cover"]:
            pin = book["cover"]["sha256"]
            content["media"]["cover"] = files.add(Books(sc).root / ident / "covers" / (pin + ".jpg"), "book_cover", pin)
    if media and kind == "dub":
        folder = sc.root / "out" / "dubs" / ident
        for role in ("source", "background"):
            saved = dubbing._read(folder / role)
            if saved:
                content["media"][role] = {
                    "asset": files.add(folder / role / saved["original_file"], role, saved["sha256"]),
                    "name": saved["name"],
                }
    return files.publish(sc.root / "out" / "portable", kind, content)


def _voice_manifest(item):
    if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not item["label"].strip():
        raise ValueError("The archive has an invalid Cast voice.")
    engine = item.get("engine_id")
    adapter = engine_adapters.get(engine) if isinstance(engine, str) else None
    options = item.get("voice_options")
    if (
        adapter is None
        or not (adapter.reference_voices or adapter.fixed_voices or adapter.preset_voices)
        or not isinstance(options, dict)
        or len(item["label"]) > 80
        or item.get("kind")
        not in {
            kind
            for kind, supported in (
                ("fixed_voice", adapter.fixed_voices),
                ("preset_voice", adapter.preset_voices),
                ("reference", adapter.reference_voices),
            )
            if supported
        }
    ):
        raise ValueError("This archive uses an unsupported Cast model or invalid voice settings.")
    pin = item.get("model")
    if not isinstance(pin, dict) or any(
        pin.get(key) is not None and not isinstance(pin[key], str) for key in ("checkpoint", "revision")
    ):
        raise ValueError("The portable voice needs its source model details.")
    if options.get("checkpoint_revision") and options["checkpoint_revision"] != pin.get("revision"):
        raise ValueError("The portable voice has conflicting model revisions.")
    return engine, options


def _sample(envelope, archive, item):
    sample = item.get("sample")
    if sample is None:
        return None
    if item["kind"] != "reference":
        raise ValueError("A preset voice file should contain its settings, without a voice recording.")
    if not isinstance(sample, dict) or not isinstance(sample.get("asset"), str):
        raise ValueError("The voice reference information is invalid.")
    role = envelope["assets"].get(sample.get("asset"), {})
    if (
        role.get("role") != "voice_reference"
        or sample.get("sha256") != role.get("sha256")
        or any(not isinstance(sample.get(key), str) or not sample[key].strip() for key in ("origin", "allowed_use"))
    ):
        raise ValueError("A voice reference needs its saved origin, permitted use and hash.")
    if not isinstance(sample.get("transcript", ""), str) or len(sample.get("transcript", "")) > MAX_REFERENCE_TEXT:
        raise ValueError("The voice recording's transcript exceeds the Lab's limit.")
    if any(len(sample[key]) > 1000 for key in ("origin", "allowed_use")):
        raise ValueError("Shorten the origin and allowed-use notes in the source Lab and export again.")
    return sample, _wav(archive, sample["asset"], 30)


def _wav(archive, name, limit):
    audio = archive.read(name)
    try:
        units = sf.info(io.BytesIO(audio))
    except RuntimeError as exc:
        raise ValueError("The included recording could not be read. Export the file again.") from exc
    if units.format != "WAV" or units.channels != 1 or not 8000 <= units.samplerate <= 96000 or not 0 < units.duration <= limit:
        raise ValueError(f"Export a playable mono WAV up to {limit} seconds and try again.")
    return audio


def _audition(envelope, archive, item):
    saved = item.get("audition")
    if saved is None:
        return None
    if (
        not isinstance(saved, dict)
        or not isinstance(saved.get("asset"), str)
        or envelope["assets"].get(saved["asset"], {}).get("role") != "voice_audition"
        or not isinstance(saved.get("settings"), dict)
    ):
        raise ValueError("The audition information does not match the included audio.")
    for key, limit in (("text", 1200), ("spoken_text", 1200), ("language", 240), ("direction", 1000)):
        value = saved.get(key)
        if not isinstance(value, str) or len(value) > limit or (key in {"text", "spoken_text"} and not value.strip()):
            raise ValueError("The audition needs its original words and settings. Export the voice file again.")
    return saved, _wav(archive, saved["asset"], 12)


def _character(contents, item):
    character = contents.get("character", {"name": item["label"], "description": "", "tags": []})
    if not isinstance(character, dict):
        raise ValueError("The voice file is missing its character details.")
    name, description, tags = character.get("name"), character.get("description"), character.get("tags")
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 80
        or not isinstance(description, str)
        or len(description) > 2000
        or not isinstance(tags, list)
        or len(tags) > 100
        or any(not isinstance(tag, str) or len(tag) > 40 for tag in tags)
    ):
        raise ValueError("The included character is invalid. Check its details and export the voice again.")
    rows = contents.get("pronunciations", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("A voice file can hold up to 100 character pronunciations.")
    limits = {"source": 100, "replacement": 200, "notes": 500}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != set(limits)
            or any(not isinstance(row[key], str) or len(row[key]) > limit for key, limit in limits.items())
            or not row["source"].strip()
            or not row["replacement"].strip()
        ):
            raise ValueError("The included pronunciations are invalid. Check them in Cast and export again.")
    if len({row["source"].strip().casefold() for row in rows}) != len(rows):
        raise ValueError("The file repeats a character pronunciation. Export the voice again.")
    return character, rows


def preview(sc, raw):
    with checked(raw) as (envelope, archive):
        kind, data = envelope["kind"], envelope["contents"]
        if kind == "persona":
            item = data.get("voice")
            _voice_manifest(item)
            sample = _sample(envelope, archive, item)
            if item["kind"] == "reference" and not sample:
                raise ValueError("Export the voice with its approved recording, then check the new file.")
            _audition(envelope, archive, item)
            character, _ = _character(data, item)
            return {
                "kind": kind,
                "title": character["name"],
                "author": data.get("author"),
                "voices": [item],
                "assets": envelope["assets"],
                "archive_sha256": _sha(raw),
            }
        items = data.get("voices")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("A project archive needs its source Cast voices.")
        for item in items:
            _voice_manifest(item)
            _id(item.get("id"))
            _sample(envelope, archive, item)
        media = data.get("media", {})
        if not isinstance(media, dict):
            raise ValueError("The archive has invalid attachment information.")
        return {
            "kind": kind,
            "title": data.get("title"),
            "voices": items,
            "chapters": len(data.get("scenes") or []),
            "media": [role for role in ("cover", "source", "background") if role in media],
            "assets": envelope["assets"],
            "archive_sha256": _sha(raw),
        }


def sample_preview(raw, asset):
    with checked(raw) as (envelope, archive):
        contents = envelope["contents"]
        items = [contents.get("voice")] if envelope["kind"] == "persona" else contents.get("voices")
        if not isinstance(items, list):
            raise ValueError("The portable archive has no voice list.")
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("audition"), dict) and item["audition"].get("asset") == asset:
                return _audition(envelope, archive, item)[1]
            if isinstance(item.get("sample"), dict) and item["sample"].get("asset") == asset:
                return _sample(envelope, archive, item)[1]
    raise KeyError("This archive has no matching voice preview.")


def _import_voice(sc, envelope, archive, item, target, permission):
    engine, options = _voice_manifest(item)
    sample = _sample(envelope, archive, item)
    audition = _audition(envelope, archive, item)
    persona = envelope["kind"] == "persona"
    character, pronunciation_rows = _character(envelope["contents"] if persona else {}, item)
    if sample and permission is not True:
        raise ValueError("Confirm that you reviewed the recording's allowed use before importing.")
    if persona and item["kind"] == "reference" and not sample:
        raise ValueError("Export the voice with its approved recording, then check the new file.")
    if item["kind"] == "preset_voice":
        options = sc.cast._validate_preset_voice_options(options)
    if sample:
        reference, raw = sample
        # Validate model-specific length and WAV decoding before creating any Cast records.
        audio, rate, duration, _ = sc.cast._decode_audio(raw, "imported-voice.wav")
        sc.cast._make_reference(audio, rate, duration, {}, REFERENCE_POLICIES[engine]["max_seconds"], speakers=sc.cast.speakers)
        if engine in TRANSCRIPT_ENGINES and not reference.get("transcript", "").strip():
            raise ValueError("This model needs the exact words in the voice recording. Fill them in before exporting.")
    source_id = item.get("id")
    if source_id is not None:
        _id(source_id)
    if target:
        wanted = _id(target)
        if all(char["id"] != wanted for char in sc.cast.list_cast()):
            raise ValueError("Choose an existing Cast character for the persona.")
    else:
        wanted = sc.cast.create_character(character["name"], character["description"], tags=character["tags"])["id"]
    pin = item["model"]
    provenance = f"Portable voice: {pin.get('checkpoint') or engine} / {pin.get('revision') or 'revision unavailable'}."
    author = envelope["contents"].get("author")
    if isinstance(author, str) and author.strip():
        provenance += f" Author: {author.strip()[:120]}."
    original = item.get("notes")
    notes = f"{original.strip()}\n{provenance}" if isinstance(original, str) and original.strip() else provenance
    entry = sc.cast.create_identity(wanted, engine, item["label"], options, notes=notes[:2000])
    uploaded = None
    if sample:
        reference, raw = sample
        uploaded = sc.cast.add_reference(
            entry["id"],
            "portable-reference.wav",
            raw,
            transcript=reference.get("transcript", ""),
            origin=reference["origin"],
            allowed_use=reference["allowed_use"],
        )
    if not target:
        for row in pronunciation_rows:
            sc.cast.save_pronunciation(character_id=wanted, **row)
    if audition:
        saved, raw = audition
        audio, rate = sf.read(io.BytesIO(raw), dtype="float32")
        voice = sc.cast.get_generation_identity(entry["id"], uploaded["id"] if uploaded else None)
        sc.cast.create_take(
            voice,
            line_id="imported-audition",
            text=saved["text"],
            spoken_text=saved["spoken_text"],
            language=saved["language"],
            direction=saved["direction"],
            settings=saved["settings"],
            model={"engine_id": engine, "checkpoint": pin.get("checkpoint"), "checkpoint_revision": pin.get("revision")},
            wav=audio,
            sample_rate=rate,
            generation_info={},
        )
    return sc.cast.get_identity(entry["id"])


def import_archive(sc, raw, request):
    if request.get("archive_sha256") != _sha(raw):
        raise ValueError("Preview this exact archive before importing.")
    with checked(raw) as (envelope, archive):
        kind, data = envelope["kind"], envelope["contents"]
        if kind == "persona":
            return {
                "kind": kind,
                "identity": _import_voice(
                    sc, envelope, archive, data.get("voice"), request.get("character_id"), request.get("permission")
                ),
            }
        source_voice = request.get("archive_voice_id")
        if source_voice:
            _id(source_voice)
            entries = data.get("voices")
            if not isinstance(entries, list):
                raise ValueError("The portable archive has no voice list.")
            for item in entries:
                if isinstance(item, dict) and item.get("id") == source_voice:
                    return {
                        "kind": "persona",
                        "identity": _import_voice(sc, envelope, archive, item, None, request.get("permission")),
                    }
            raise ValueError("This archive has no matching source voice.")
        return _import_project(sc, envelope, archive, request)


def _import_project(sc, envelope, archive, request):
    kind, data = envelope["kind"], envelope["contents"]
    entries, mapping = data.get("scenes"), request.get("voices")
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= 160
        or not isinstance(mapping, dict)
        or (kind != "book" and len(entries) != 1)
    ):
        raise ValueError("The project needs its chapter or scene data and Cast assignments.")
    if not isinstance(data.get("voices"), list) or not 1 <= len(data["voices"]) <= 100:
        raise ValueError("The archive has no Cast voice list.")
    expected = set()
    for row in data["voices"]:
        _voice_manifest(row)
        key = _id(row.get("id"))
        if key in expected:
            raise ValueError("Duplicate source Cast identity in the project.")
        expected.add(key)
    if set(mapping) != expected:
        raise ValueError("Assign every source voice to an approved voice in this library.")
    chosen = {key: sc.cast.get_generation_identity(_id(ident)) for key, ident in mapping.items()}
    if any(voice["status"] != "approved" for voice in chosen.values()):
        raise ValueError("Every mapped voice must be approved in this library.")
    book_id = new_id() if kind == "book" else None
    source_id = _id(data.get("id"))
    scenes, line_count = [], 0
    for saved in entries:
        if not isinstance(saved, dict) or not isinstance(saved.get("lines"), list):
            raise ValueError("Unreadable portable scene data.")
        source_scene = _id(saved.get("source_scene_id"))
        lines = []
        for row in saved["lines"]:
            if not isinstance(row, dict) or not isinstance(row.get("voice_id"), str) or row["voice_id"] not in chosen:
                raise ValueError("A passage lacks its mapped voice.")
            voice = chosen[row["voice_id"]]
            clean = {key: value for key, value in row.items() if key in LINE_FIELDS}
            clean.update(identity_id=voice["id"], character_id=voice["character_id"])
            lines.append(clean)
        line_count += len(lines)
        if line_count > 6000:
            raise ValueError("The imported project exceeds 6,000 passages.")
        payload = {key: value for key, value in saved.items() if key in SCENE_FIELDS}
        payload.update(lines=lines, imported_from={"project_id": source_id, "scene_id": source_scene})
        if book_id:
            payload["book_id"] = book_id
        scenes.append(sc._normalize_scene(payload, new_id(), 1))
    if kind == "dub" and not scenes[0].get("timeline"):
        raise ValueError("The imported dub has no timed source track.")
    if request.get("import_voice_samples") is True:
        raise ValueError("Import voice samples as individual candidates for review before assigning them to a project.")
    assets = data.get("media")
    roles = {"cover"} if book_id else {"source", "background"} if kind == "dub" else set()
    if not isinstance(assets, dict) or set(assets) - roles:
        raise ValueError("Unexpected media in this project type.")
    if not assets or request.get("include_media") is not True:
        assets = {}
    stamp, book, books = utc_now(), None, None
    if book_id:
        books = Books(sc)
        saved = data.get("book")
        if not isinstance(saved, dict) or not isinstance(saved.get("pronunciations"), list):
            raise ValueError("The archive has incomplete book metadata.")
        entries = saved["pronunciations"]
        if any(not isinstance(row, dict) or not isinstance(row.get("source"), str) for row in entries):
            raise ValueError("The portable book pronunciation rules are invalid.")
        rules = pronunciations({row["source"]: row.get("replacement") for row in entries})
        book = {
            "schema_version": 1,
            "id": book_id,
            "revision": 1,
            "chapters": [item["scene_id"] for item in scenes],
            "metadata": metadata(saved.get("metadata")),
            "created": stamp,
            "source": saved.get("source", {}),
            "gap_ms": milliseconds(saved.get("gap_ms")),
            "pronunciations": rules,
            "cover": None,
            "imported_from": source_id,
        }
    with tempfile.TemporaryDirectory(dir=sc.root, prefix=".project-import-") as tmp:
        root = Path(tmp)
        target = None
        if "cover" in assets:
            key = assets["cover"]
            receipt = envelope["assets"].get(key, {})
            if receipt.get("role") != "book_cover":
                raise ValueError("The book cover does not match its asset receipt.")
            folder = root / "publication" / "covers"
            folder.mkdir(parents=True)
            cover = folder / (receipt["sha256"] + ".jpg")
            cover.write_bytes(archive.read(key))
            artifact = book_export._probe(cover, "jpeg_pipe")
            streams = artifact["streams"]
            if len(streams) != 1 or any(not 0 < streams[0].get(dim, 0) <= 5000 for dim in ("width", "height")):
                raise ValueError("Invalid cover dimensions.")
            book["cover"] = {"sha256": receipt["sha256"], "original_sha256": receipt["sha256"]}
            target = books.root / book_id
        if kind == "dub" and assets:
            target = sc.root / "out" / "dubs" / scenes[0]["scene_id"]
            for role, info in assets.items():
                if not isinstance(info, dict) or not isinstance(info.get("asset"), str):
                    raise ValueError("Invalid dub attachment metadata.")
                key, name = info.get("asset"), info.get("name")
                receipt = envelope["assets"].get(key, {})
                if (
                    receipt.get("role") != role
                    or not isinstance(name, str)
                    or not name
                    or len(name) > 255
                    or Path(name).name != name
                ):
                    raise ValueError("A dub attachment has incomplete source information.")
                suffix = Path(key).suffix
                if suffix not in dubbing.FORMATS:
                    raise ValueError("Unsupported dub attachment format.")
                source = scenes[0].get("source_media") if role == "source" else None
                if source and (not isinstance(source, dict) or source.get("sha256") != receipt["sha256"]):
                    raise ValueError("The included original does not match the timed transcript.")
                folder = root / "publication" / role
                folder.mkdir(parents=True)
                upload = folder / ("upload" + suffix)
                with archive.open(key) as read, upload.open("wb") as write:
                    shutil.copyfileobj(read, write, length=256 * 1024)
                prepared = dubbing._prepare(upload, folder, dubbing.FORMATS[suffix])
                if role == "background" and (prepared["video"] or not prepared["audio"]):
                    raise ValueError("The background entry must be audio-only.")
                dubbing.write_json_atomic(
                    folder / "manifest.json",
                    {
                        **prepared,
                        "name": name,
                        "size_bytes": receipt["bytes"],
                        "sha256": receipt["sha256"],
                        "original_file": upload.name,
                        "transcript_source_verified": bool(source),
                    },
                )
        with sc._connect() as db:
            for scene in scenes:
                sc._insert_scene(db, scene, stamp)
            if book:
                db.execute("INSERT INTO books VALUES(?,?,?,?)", (book_id, canonical_json(book), digest_json(book), stamp))
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                (root / "publication").rename(target)
    for scene in scenes:
        sc._write_scene_file(scene)
    return {
        "kind": kind,
        "project_id": book_id or scenes[0]["scene_id"],
        "scene_ids": [part["scene_id"] for part in scenes],
        "included_media": bool(assets),
        "voice_samples_available": any(row.get("sample") for row in data["voices"]),
    }
