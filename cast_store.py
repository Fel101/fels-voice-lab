"""Persistent, append-only character, reference-version, and performance-take store."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import median_filter
from scipy.signal import istft, resample_poly, stft

import ffmpeg_tools
import loudness
from db_snapshots import snapshot_before_migration
from evaluation_diagnostics import TAKE_CHECKER, take_checks
import engine_adapters
import reference_quality
import speaker_similarity
import synthetic_label
import take_edit


LAB = Path(__file__).resolve().parent
DATABASE = LAB / "data" / "cast" / "cast.sqlite3"
CAST_SCHEMA_VERSION = 9
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_SOURCE_SECONDS = 600.0
MIN_REFERENCE_SECONDS = 0.5
MAX_REFERENCE_SECONDS = 60.0
MAX_REFERENCE_TEXT = 1200
MAX_REFERENCE_CHANNELS = 8
MAX_REFERENCE_RATE = 192_000
# MP3 is read by libsndfile; M4A is an MP4 container that only FFmpeg reads.
FFMPEG_AUDIO = {".m4a"}
SUPPORTED_AUDIO = {".wav", ".flac", ".ogg", ".mp3"} | FFMPEG_AUDIO
ID_RE = re.compile(r"\A[a-f0-9]{32}\Z")
REFERENCE_ENGINES = {"turbo", "orig", "breeze-tts-2", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base", "voxcpm2", 'drama-3'}
PRESET_VOICE_ENGINES = {"qwen3-tts-1.7b-custom-voice", *engine_adapters.engines_where("preset_voices")}
TRANSCRIPT_ENGINES = {"breeze-tts-2", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base", 'drama-3'}
DIRECTION_ENGINES = {"breeze-tts-2", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base",
                     "qwen3-tts-1.7b-custom-voice", "voxcpm2", 'drama-3'}
_CAST_SCHEMA_LOCK = threading.RLock()
REFERENCE_POLICIES = {
    'drama-3': {'max_seconds':60.,'recommended_rate':44100,'requires_transcript_for_cast':True,
                'limit_basis':'Fel\'s Voice Lab reference ceiling; native voice files require exact transcripts'},
    "turbo": {"max_seconds": 60.0, "limit_basis": "voice-lab safety ceiling; no stricter local native limit was established",
              "recommended_rate": 24_000, "requires_transcript_for_cast": True},
    "orig": {"max_seconds": 60.0, "limit_basis": "voice-lab safety ceiling; no stricter local native limit was established",
             "recommended_rate": 24_000, "requires_transcript_for_cast": True},
    "breeze-tts-2": {"max_seconds": 60.0, "limit_basis": "native Breeze MAX_REFERENCE_SECONDS",
                     "recommended_rate": 24_000, "requires_transcript_for_cast": True},
    "qwen3-tts-1.7b-base": {"max_seconds": 60.0, "limit_basis": "voice-lab safety ceiling; the installed Qwen callable exposes no hard duration limit",
                            "recommended_rate": 24_000, "requires_transcript_for_cast": True},
    "qwen3-tts-0.6b-base": {"max_seconds": 60.0, "limit_basis": "voice-lab safety ceiling; the installed Qwen callable exposes no hard duration limit",
                            "recommended_rate": 24_000, "requires_transcript_for_cast": True},
    "voxcpm2": {"max_seconds": 60.0, "limit_basis": "voice-lab safety ceiling for reference conditioning",
                "recommended_rate": 48_000, "requires_transcript_for_cast": False},
}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


def _validate_id(value, what="id"):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {what}.")
    return value


def generation_identity_snapshot(identity):
    """Stable, JSON-safe voice identity inputs used to validate scene selections."""
    return {
        "identity_type": identity["kind"],
        "engine_id": identity["engine_id"],
        "engine_options": identity.get("engine_options", {}),
    }


def _safe_relative(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("Stored media path escaped the local voice-lab data folder.")
    return target


REFERENCE_LENGTH = "Source audio duration must be {minimum:g}–{maximum:g} seconds; trim it before saving a reference version."


def decode_audio(source_bytes, filename, *, what="Reference", max_seconds=None, length=REFERENCE_LENGTH):
    """Decode an uploaded recording into float32 samples shaped [frames, channels]: WAV, FLAC, OGG and MP3 with soundfile,
    M4A with FFmpeg, as `filename`'s extension says (nothing else of the name is used). It refuses an empty file, one over
    MAX_UPLOAD_BYTES, a rate outside 8–192 kHz, more than MAX_REFERENCE_CHANNELS channels, and a length outside
    MIN_REFERENCE_SECONDS to max_seconds (MAX_SOURCE_SECONDS, read when it is called, unless given), which `length` says
    with {minimum} and {maximum}. `what` names the recording in the other refusals. Cast imports call it for a reference's
    source recording; Voice conversion's uploads with their own limits. Returns (audio, sample rate, seconds, extension)."""
    max_seconds = MAX_SOURCE_SECONDS if max_seconds is None else max_seconds
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_AUDIO:
        raise ValueError(f"{what} files must be WAV, FLAC, OGG, MP3 or M4A audio.")
    if not source_bytes or len(source_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{what} audio must be between 1 byte and {MAX_UPLOAD_BYTES // (1024*1024)} MiB.")

    def check_format(rate, channels):
        if rate < 8_000 or rate > MAX_REFERENCE_RATE:
            raise ValueError(f"{what} sample rate {rate} Hz is outside 8–192 kHz.")
        if channels < 1 or channels > MAX_REFERENCE_CHANNELS:
            raise ValueError(f"{what} audio must have 1–{MAX_REFERENCE_CHANNELS} channels.")

    def check_length(seconds):
        if seconds < MIN_REFERENCE_SECONDS or seconds > max_seconds:
            raise ValueError(length.format(minimum=MIN_REFERENCE_SECONDS, maximum=max_seconds))

    if suffix in FFMPEG_AUDIO:
        # FFmpeg is the only reader, and it decodes at most just past the ceiling. The rate, channels and length it
        # reports are checked like any other file's.
        audio, sample_rate = ffmpeg_tools.decode_audio(source_bytes, suffix, max_seconds)
        check_format(sample_rate, audio.shape[1])
        duration = audio.shape[0] / sample_rate
    elif suffix == ".mp3":
        # An MP3 header only estimates its length, so the length is measured on the samples, and no more than just
        # past the ceiling is read, whatever the header claims.
        try:
            with sf.SoundFile(io.BytesIO(source_bytes)) as mp3:
                sample_rate = mp3.samplerate
                check_format(sample_rate, mp3.channels)
                audio = mp3.read(int(max_seconds * sample_rate) + 1, dtype="float32", always_2d=True)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not read {what.lower()} audio: {exc}") from exc
        duration = audio.shape[0] / sample_rate
    else:
        try:
            info = sf.info(io.BytesIO(source_bytes))
            duration = float(info.duration)
        except Exception as exc:
            raise ValueError(f"Could not read {what.lower()} audio: {exc}") from exc
        check_format(info.samplerate, info.channels)
        check_length(duration)
        try:
            audio, sample_rate = sf.read(io.BytesIO(source_bytes), dtype="float32", always_2d=True)
        except Exception as exc:
            raise ValueError(f"Could not decode {what.lower()} audio: {exc}") from exc
    check_length(duration)
    if audio.size == 0 or not np.isfinite(audio).all():
        raise ValueError(f"{what} audio is empty or contains non-finite samples.")
    return audio, int(sample_rate), duration, suffix


AUDIO_SUFFIXES = SUPPORTED_AUDIO
MAX_EXTERNAL_REPORT = 200
RETIREMENT_KINDS = ("character", "identity", "reference", "take")
_TAKE_SELECT = ("SELECT t.*,c.name AS character_name,i.label AS identity_label,i.engine_id FROM takes t "
                "JOIN characters c ON c.id=t.character_id JOIN identities i ON i.id=t.identity_id")
_SOURCE_LABELS = {"character": "its character", "identity": "its voice",
                  "reference": "its reference recording", "take": "its source take"}
_CHILD_QUERIES = {
    "character": (("identity", "SELECT id FROM identities WHERE character_id=?"),),
    "identity": (("reference", "SELECT id FROM reference_versions WHERE identity_id=?"),
                 ("take", "SELECT id FROM takes WHERE identity_id=?")),
    "reference": (("take", "SELECT id FROM takes WHERE reference_version_id=?"),
                  ("reference", "SELECT id FROM reference_versions WHERE parent_reference_id=?")),
    "take": (("reference", "SELECT id FROM reference_versions WHERE parent_take_id=?"),
             ("take", "SELECT id FROM takes WHERE parent_take_id=?")),
}


_TAKE_LINKS = "SELECT identity_id,reference_version_id,parent_take_id FROM takes WHERE id=?"
_TAKE_LINKS_BEFORE_EDITS = "SELECT identity_id,reference_version_id,NULL FROM takes WHERE id=?"


def _retirement_target(kind, item_id):
    if kind not in RETIREMENT_KINDS:
        raise ValueError("Retirement target must be a character, voice, reference recording, or take.")
    return kind, _validate_id(item_id, f"{kind} id")


class RetirementReader:
    """Effective retirement on one database connection.

    An item is retired while it has an active retirement of its own, or while a
    character, voice, source recording or take it was made from is retired.
    """

    def __init__(self, db):
        self.db = db
        self.own = {(row[1], row[2]): {"reason": row[3], "retired_at": row[4]} for row in db.execute(
            "SELECT id,target_kind,target_id,reason,retired_at FROM retirements WHERE restored_at IS NULL")}
        self._parents, self._found = {}, {}
        # The exporter reads a library read-only before the Lab has migrated it, so a takes table may predate schema 9,
        # when an edited take began to name its parent.
        edits = any(column[1] == "parent_take_id" for column in db.execute("PRAGMA table_info(takes)"))
        self._take_links = _TAKE_LINKS if edits else _TAKE_LINKS_BEFORE_EDITS

    def _parents_of(self, kind, item_id):
        key = (kind, item_id)
        if key not in self._parents:
            links = []
            if kind == "identity":
                row = self.db.execute("SELECT character_id FROM identities WHERE id=?", (item_id,)).fetchone()
                links = [("character", row[0])] if row else []
            elif kind == "reference":
                row = self.db.execute("SELECT identity_id,parent_reference_id,parent_take_id FROM reference_versions WHERE id=?",
                                      (item_id,)).fetchone()
                if row:
                    links = [("identity", row[0])]
                    links += [("reference", row[1])] if row[1] else []
                    links += [("take", row[2])] if row[2] else []
            elif kind == "take":
                row = self.db.execute(self._take_links, (item_id,)).fetchone()
                if row:
                    links = [("identity", row[0])] + ([("reference", row[1])] if row[1] else [])
                    links += [("take", row[2])] if row[2] else []
            self._parents[key] = links
        return self._parents[key]

    def _source(self, kind, item_id):
        key = (kind, item_id)
        if key not in self._found:
            self._found[key] = None  # Breaks any cycle while the walk is in progress.
            if key in self.own:
                found = (key, self.own[key])
            else:
                found = None
                for parent_kind, parent_id in self._parents_of(kind, item_id):
                    found = self._source(parent_kind, parent_id)
                    if found is not None:
                        break
            self._found[key] = found
        return self._found[key]

    def state(self, kind, item_id):
        """Retirement details for one item, or None while it is usable."""
        found = self._source(kind, item_id)
        if found is None:
            return None
        (source_kind, source_id), record = found
        return {"reason": record["reason"], "retired_at": record["retired_at"], "source_kind": source_kind,
                "source_id": source_id, "source_label": _SOURCE_LABELS[source_kind],
                "inherited": (source_kind, source_id) != (kind, item_id)}


def _retirement_fields(reader, kind, item_id):
    state = reader.state(kind, item_id)
    return {"retired": state is not None, "retirement": state}


def _dependents(db, kind, item_id):
    """The item and everything made from it, grouped by kind."""
    found = {name: set() for name in RETIREMENT_KINDS}
    found[kind].add(item_id)
    pending = [(kind, item_id)]
    while pending:
        current_kind, current_id = pending.pop()
        for child_kind, sql in _CHILD_QUERIES[current_kind]:
            for (child_id,) in db.execute(sql, (current_id,)).fetchall():
                if child_id not in found[child_kind]:
                    found[child_kind].add(child_id)
                    pending.append((child_kind, child_id))
    return found


def _in_chunks(ids):
    ordered = sorted(ids)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        yield chunk, ",".join("?" * len(chunk))


def _select_in(db, select, column, ids):
    rows = []
    for chunk, marks in _in_chunks(ids):
        rows += db.execute(f"{select} WHERE {column} IN ({marks})", chunk).fetchall()
    return rows


def _delete_in(db, table, column, ids):
    for chunk, marks in _in_chunks(ids):
        db.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", chunk)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _waveform_checks(path, expected_text):
    """Record advisory take checks. A failed check is stored as unavailable and never discards the take."""
    try:
        return take_checks(path, expected_text=expected_text)
    except Exception as exc:
        return {"status": "unavailable", "checker": TAKE_CHECKER, "flags": [], "error": str(exc)}


def _stored_waveform_checks(raw):
    """Takes saved before checks existed have no record; say so instead of reporting a clean result."""
    if not raw:
        return {"status": "not_recorded", "checker": None, "flags": []}
    return json.loads(raw)


def _loudness_readings(path):
    """Loudness and peaks of a saved take. Like the waveform checks, a failure is stored as unavailable and never
    discards the take."""
    try:
        return loudness.measure_file(path)
    except Exception as exc:
        return loudness.unavailable(exc)


class CastStore:
    """SQLite metadata plus immutable local source audio, reference versions, and generated takes."""

    def __init__(self, root: Path | str = LAB):
        self.root = Path(root).resolve()
        self.data_root = self.root / "data" / "cast"
        self.db_path = self.data_root / "cast.sqlite3"
        # The speaker check in the reference screen needs the proxy encoder; without it the check reports as not checked.
        self.speakers = speaker_similarity if speaker_similarity.encoder_available() else None

    @contextmanager
    def _connect(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            # Initial browser requests open Cast concurrently. Serialize the
            # version check and table creation so first readers cannot both
            # observe version zero and race to create the same schema.
            with _CAST_SCHEMA_LOCK:
                snapshot_before_migration(db, self.db_path, db.execute("PRAGMA user_version").fetchone()[0],
                                          CAST_SCHEMA_VERSION, "cast")
                self._migrate(db)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _step(db, start, end, statements):
        """Run one schema step as a single transaction, so a crash can never leave a new column under the old version
        number. The version is read again once the write lock is held: another process may have finished this step
        while this one waited, and then there is nothing left to do."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] == start:
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={end}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return end

    @staticmethod
    def _migrate(db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > CAST_SCHEMA_VERSION:
            raise RuntimeError(f"Cast database schema {version} is newer than this voice-lab build.")
        if version == 0:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE characters (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE identities (
                    id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL REFERENCES characters(id),
                    engine_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected')),
                    kind TEXT NOT NULL CHECK(kind IN ('reference','fixed_voice')),
                    engine_options_json TEXT NOT NULL DEFAULT '{}',
                    notes TEXT NOT NULL DEFAULT '',
                    approved_reference_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(approved_reference_id) REFERENCES reference_versions(id)
                );
                CREATE UNIQUE INDEX one_approved_identity_per_engine
                    ON identities(character_id, engine_id) WHERE status='approved';
                CREATE TABLE reference_versions (
                    id TEXT PRIMARY KEY,
                    identity_id TEXT NOT NULL REFERENCES identities(id),
                    version INTEGER NOT NULL,
                    source_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    audio_sha256 TEXT NOT NULL,
                    transcript TEXT NOT NULL DEFAULT '',
                    sample_rate INTEGER NOT NULL,
                    channels INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    peak_dbfs REAL NOT NULL,
                    rms_dbfs REAL NOT NULL,
                    clipped_samples INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    allowed_use TEXT NOT NULL,
                    parent_reference_id TEXT REFERENCES reference_versions(id),
                    parent_take_id TEXT,
                    edit_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(identity_id, version)
                );
                CREATE INDEX reference_versions_identity ON reference_versions(identity_id, version DESC);
                CREATE TABLE takes (
                    id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL REFERENCES characters(id),
                    identity_id TEXT NOT NULL REFERENCES identities(id),
                    reference_version_id TEXT REFERENCES reference_versions(id),
                    line_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    spoken_text TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    settings_json TEXT NOT NULL,
                    model_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    audio_sha256 TEXT NOT NULL,
                    sample_rate INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','selected','rejected')),
                    rating INTEGER CHECK(rating IS NULL OR (rating >= 1 AND rating <= 5)),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX takes_line ON takes(identity_id, line_id, created_at);
                CREATE TABLE pronunciations (
                    id TEXT PRIMARY KEY,
                    character_id TEXT REFERENCES characters(id),
                    source TEXT NOT NULL,
                    replacement TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(character_id, source COLLATE NOCASE)
                );
                PRAGMA user_version=1;
                COMMIT;
            """)
            version = 1
        if version == 1:
            version = CastStore._step(db, 1, 2, [
                "ALTER TABLE takes ADD COLUMN request_key TEXT",
                "CREATE UNIQUE INDEX takes_request_key ON takes(request_key) WHERE request_key IS NOT NULL"])
        if version == 2:
            # Add a real identity kind for checkpoint-native presets while preserving all
            # existing identity and reference rows. Referencing tables continue to target
            # the `identities` name throughout the replacement.
            db.execute("PRAGMA foreign_keys=OFF")
            try:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE identities_next (
                        id TEXT PRIMARY KEY,
                        character_id TEXT NOT NULL REFERENCES characters(id),
                        engine_id TEXT NOT NULL,
                        label TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected')),
                        kind TEXT NOT NULL CHECK(kind IN ('reference','fixed_voice','preset_voice')),
                        engine_options_json TEXT NOT NULL DEFAULT '{}',
                        notes TEXT NOT NULL DEFAULT '',
                        approved_reference_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(approved_reference_id) REFERENCES reference_versions(id)
                    );
                    INSERT INTO identities_next SELECT * FROM identities;
                    DROP TABLE identities;
                    ALTER TABLE identities_next RENAME TO identities;
                    CREATE UNIQUE INDEX one_approved_identity_per_engine
                        ON identities(character_id, engine_id) WHERE status='approved';
                    PRAGMA user_version=3;
                    COMMIT;
                """)
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("PRAGMA foreign_keys=ON")
            version = 3
        if version == 3:
            # Existing takes keep an explicit not-recorded state; only takes created after this migration carry checks.
            version = CastStore._step(db, 3, 4, [
                "ALTER TABLE takes ADD COLUMN waveform_checks_json TEXT NOT NULL DEFAULT ''"])
        if version == 4:
            # Retirement is an append-only record. A purge keeps a tombstone with hashes, never the audio.
            version = CastStore._step(db, 4, 5, [
                """CREATE TABLE IF NOT EXISTS retirements (
                    id TEXT PRIMARY KEY,
                    target_kind TEXT NOT NULL CHECK(target_kind IN ('character','identity','reference','take')),
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    retired_at TEXT NOT NULL,
                    restored_at TEXT
                )""",
                "CREATE UNIQUE INDEX IF NOT EXISTS retirements_active ON retirements(target_kind,target_id) "
                "WHERE restored_at IS NULL",
                """CREATE TABLE IF NOT EXISTS purge_batches (
                    id TEXT PRIMARY KEY,
                    root_kind TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    purged_at TEXT NOT NULL,
                    counts_json TEXT NOT NULL DEFAULT '{}',
                    external_copies_json TEXT NOT NULL DEFAULT '[]'
                )""",
                """CREATE TABLE IF NOT EXISTS purged_items (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES purge_batches(id),
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    sha256s_json TEXT NOT NULL DEFAULT '[]'
                )""",
                "CREATE INDEX IF NOT EXISTS purged_items_target ON purged_items(target_kind,target_id)"])
        if version == 5:
            # Derived forced-alignment records. Takes made before this have no record until they are aligned.
            version = CastStore._step(db, 5, 6, [
                "ALTER TABLE takes ADD COLUMN word_timings_json TEXT NOT NULL DEFAULT ''"])
        if version == 6:
            # Kokoro's own phoneme times, stored when a take is made. Takes made before this have none.
            version = CastStore._step(db, 6, 7, [
                "ALTER TABLE takes ADD COLUMN phoneme_timings_json TEXT NOT NULL DEFAULT ''"])
        if version == 7:
            # Loudness and peak readings, stored when a take is made. Takes made before this have none until
            # measure_missing_loudness reads their audio.
            version = CastStore._step(db, 7, 8, [
                "ALTER TABLE takes ADD COLUMN loudness_json TEXT NOT NULL DEFAULT ''"])
        if version == 8:
            # An edited take names the take it was made from (a soft link, like a reference's parent_take_id) and keeps the
            # edit that was applied. Takes made before this are all originals: no parent and no edit.
            CastStore._step(db, 8, 9, [
                "ALTER TABLE takes ADD COLUMN parent_take_id TEXT",
                "ALTER TABLE takes ADD COLUMN edit_json TEXT NOT NULL DEFAULT ''",
                "CREATE INDEX takes_parent ON takes(parent_take_id) WHERE parent_take_id IS NOT NULL"])

    def _row(self, query, args=()):
        with self._connect() as db:
            return db.execute(query, args).fetchone()

    def _rows(self, query, args=()):
        with self._connect() as db:
            return db.execute(query, args).fetchall()

    def create_character(self, name, description="", tags=None):
        name = str(name or "").strip()
        description = str(description or "").strip()
        if not name or len(name) > 80:
            raise ValueError("Character name must contain 1–80 characters.")
        if len(description) > 2000:
            raise ValueError("Character description must be 2000 characters or fewer.")
        if tags is None:
            tags = []
        if not isinstance(tags, list) or any(not isinstance(tag, str) or len(tag) > 40 for tag in tags):
            raise ValueError("Character tags must be a list of text values up to 40 characters each.")
        char_id, stamp = new_id(), now_utc()
        with self._connect() as db:
            db.execute("INSERT INTO characters VALUES(?,?,?,?,?,?)",
                       (char_id, name, description, json.dumps(tags, ensure_ascii=False), stamp, stamp))
        return self.get_character(char_id)

    def update_character(self, char_id, changes):
        char_id = _validate_id(char_id, "character id")
        current = self._row("SELECT * FROM characters WHERE id=?", (char_id,))
        if current is None:
            raise KeyError("Character not found.")
        name = str(changes.get("name", current["name"]) or "").strip()
        description = str(changes.get("description", current["description"]) or "").strip()
        if not name or len(name) > 80 or len(description) > 2000:
            raise ValueError("Character name must be 1–80 characters and description at most 2000 characters.")
        with self._connect() as db:
            db.execute("UPDATE characters SET name=?,description=?,updated_at=? WHERE id=?",
                       (name, description, now_utc(), char_id))
        return self.get_character(char_id)

    def create_identity(self, char_id, engine_id, label, engine_options=None, notes=""):
        char_id = _validate_id(char_id, "character id")
        if self._row("SELECT id FROM characters WHERE id=?", (char_id,)) is None:
            raise KeyError("Character not found.")
        if self.retirement_state("character", char_id):
            raise ValueError("This character is retired; restore it before adding voices.")
        if engine_id not in REFERENCE_ENGINES and engine_id not in PRESET_VOICE_ENGINES and engine_id != "kokoro":
            raise ValueError("A voice identity must use a reference-cloning engine or Kokoro's fixed voice selection.")
        label = str(label or "").strip()
        if not label or len(label) > 80 or len(str(notes or "")) > 2000:
            raise ValueError("Voice identity label must contain 1–80 characters and notes at most 2000 characters.")
        options = engine_options or {}
        if not isinstance(options, dict):
            raise ValueError("engine_options must be an object.")
        kind = ("fixed_voice" if engine_id == "kokoro" else
                "preset_voice" if engine_id in PRESET_VOICE_ENGINES else "reference")
        if kind == "preset_voice":
            options = self._validate_preset_voice_options(options)
        identity_id, stamp = new_id(), now_utc()
        with self._connect() as db:
            db.execute("INSERT INTO identities(id,character_id,engine_id,label,status,kind,engine_options_json,notes,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (identity_id, char_id, engine_id, label, "candidate", kind,
                        json.dumps(options, ensure_ascii=False), str(notes or ""), stamp, stamp))
        return self.get_identity(identity_id)

    @staticmethod
    def _validate_preset_voice_options(options):
        if not isinstance(options, dict):
            raise ValueError("Preset voice engine_options must be an object.")
        required = ("checkpoint", "checkpoint_revision", "speaker", "language")
        if any(not isinstance(options.get(key), str) or not options[key].strip() for key in required):
            raise ValueError("A preset voice needs its checkpoint, revision, speaker, and language.")
        speakers = options.get("supported_speakers")
        languages = options.get("supported_languages")
        if (not isinstance(speakers, list) or options["speaker"] not in speakers or
                not isinstance(languages, list) or options["language"] not in languages):
            raise ValueError("Choose a speaker and language reported by the model's installed checkpoint.")
        direction = options.get("default_direction", "")
        if not isinstance(direction, str) or len(direction) > 1000:
            raise ValueError("Preset delivery instruction must be text of 1000 characters or fewer.")
        if not isinstance(options.get("settings_schema", []), list):
            raise ValueError("Preset supported settings must be a list.")
        return options

    def update_identity(self, identity_id, changes, *, fixed_voices=None):
        identity_id = _validate_id(identity_id, "identity id")
        row = self._row("SELECT * FROM identities WHERE id=?", (identity_id,))
        if row is None:
            raise KeyError("Voice identity not found.")
        label = str(changes.get("label", row["label"]) or "").strip()
        notes = str(changes.get("notes", row["notes"]) or "")
        if not label or len(label) > 80 or len(notes) > 2000:
            raise ValueError("Identity label must contain 1–80 characters and notes at most 2000 characters.")
        options = json.loads(row["engine_options_json"])
        if "engine_options" in changes:
            if not isinstance(changes["engine_options"], dict):
                raise ValueError("engine_options must be an object.")
            if row["kind"] == "preset_voice":
                raise ValueError("Preset checkpoint, speaker, and language are immutable; create a new voice identity to change them.")
            options = changes["engine_options"]
        if row["kind"] == "fixed_voice":
            voice = str(options.get("voice", ""))
            voices = [item.strip() for item in voice.split(",")]
            if (not voices or len(voices) > 8 or len(set(voices)) != len(voices) or
                    any(not item for item in voices) or
                    fixed_voices is not None and any(item not in fixed_voices for item in voices)):
                raise ValueError("Choose one or more fixed voices reported by the local service; blend IDs with commas.")
            options["voice"] = ",".join(sorted(voices))
        status = changes.get("status", row["status"])
        if status not in ("candidate", "approved", "rejected"):
            raise ValueError("Identity status must be candidate, approved, or rejected.")
        selected_reference = changes.get("reference_version_id", row["approved_reference_id"])
        if status == "approved" and row["kind"] == "reference":
            selected_reference = _validate_id(selected_reference, "reference version id")
            ref = self._row("SELECT * FROM reference_versions WHERE id=? AND identity_id=?",
                            (selected_reference, identity_id))
            if ref is None:
                raise ValueError("Approve a reference version belonging to this identity.")
            if self.retirement_state("reference", selected_reference) or self.retirement_state("identity", identity_id):
                raise ValueError("This voice or reference recording is retired; restore it before approving.")
            if REFERENCE_POLICIES.get(row["engine_id"], {}).get("requires_transcript_for_cast", True) and not ref["transcript"].strip():
                raise ValueError("Add the exact reference transcript before approving this voice.")
            self._validate_reference(ref, row["engine_id"])
        elif row["kind"] in {"fixed_voice", "preset_voice"}:
            selected_reference = None
        elif status != "approved":
            selected_reference = None
        stamp = now_utc()
        with self._connect() as db:
            if status == "approved":
                db.execute("UPDATE identities SET status='candidate', approved_reference_id=NULL, updated_at=? "
                           "WHERE character_id=? AND engine_id=? AND status='approved' AND id<>?",
                           (stamp, row["character_id"], row["engine_id"], identity_id))
            db.execute("UPDATE identities SET label=?,notes=?,status=?,engine_options_json=?,approved_reference_id=?,updated_at=? WHERE id=?",
                       (label, notes, status, json.dumps(options, ensure_ascii=False), selected_reference, stamp, identity_id))
        return self.get_identity(identity_id)

    @staticmethod
    def _validate_reference(ref, engine_id):
        if engine_id in TRANSCRIPT_ENGINES and not ref["transcript"].strip():
            raise ValueError(f"{engine_id} requires an exact reference transcript before generation.")
        if REFERENCE_POLICIES.get(engine_id, {}).get("requires_transcript_for_cast", True) and not ref["transcript"].strip():
            raise ValueError("Add the exact reference transcript before using this identity.")
        policy = REFERENCE_POLICIES.get(engine_id, {"max_seconds": MAX_REFERENCE_SECONDS, "limit_basis": "studio policy"})
        maximum = policy["max_seconds"]
        if not MIN_REFERENCE_SECONDS <= float(ref["duration_seconds"]) <= maximum:
            raise ValueError(f"Reference duration must be between {MIN_REFERENCE_SECONDS:g} and "
                             f"{maximum:g} seconds before generation ({policy['limit_basis']}).")

    def _relative(self, path):
        return Path(path).resolve().relative_to(self.root).as_posix()

    def _media_path(self, relative):
        path = _safe_relative(self.root, relative)
        if not path.is_file():
            raise FileNotFoundError("Local cast media file is missing.")
        return path

    @staticmethod
    def _decode_audio(source_bytes, filename):
        return decode_audio(source_bytes, filename)

    @staticmethod
    def _make_reference(audio, sample_rate, duration, edits, maximum_seconds, speakers=None):
        if not isinstance(edits, dict):
            raise ValueError("Reference edits must be an object.")
        try:
            start = float(edits.get("start_seconds", 0.0))
            end = float(edits.get("end_seconds", duration))
            target_rate = int(edits.get("target_sample_rate", sample_rate))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Reference trim and sample-rate settings must be numeric.") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > duration + 0.02:
            raise ValueError("Reference trim must be a valid start/end range inside the source audio.")
        cropped = audio[round(start * sample_rate):round(end * sample_rate), :]
        if not cropped.size:
            raise ValueError("The selected reference trim contains no audio samples.")
        channel_choice = edits.get("channel", "mono")
        if channel_choice == "mono":
            cropped = cropped.mean(axis=1, keepdims=True)
        elif channel_choice in ("left", "right"):
            index = 0 if channel_choice == "left" else min(1, cropped.shape[1] - 1)
            cropped = cropped[:, index:index + 1]
        else:
            raise ValueError("Reference channel must be mono, left, or right.")
        noise_settings = edits.get("noise_reduction", {})
        if not isinstance(noise_settings, dict):
            raise ValueError("Noise-reduction settings must be an object.")
        noise_enabled = noise_settings.get("enabled", False)
        if not isinstance(noise_enabled, bool):
            raise ValueError("Noise-reduction enabled must be true or false.")
        if noise_enabled:
            try:
                profile_start = float(noise_settings.get("profile_start_seconds", 0.0))
                profile_end = float(noise_settings.get("profile_end_seconds", 0.3))
                strength = float(noise_settings.get("strength", 0.75))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Noise profile and reduction strength must be numeric.") from exc
            clip_duration = cropped.shape[0] / sample_rate
            if (not all(math.isfinite(value) for value in (profile_start, profile_end, strength)) or
                    profile_start < 0 or profile_end <= profile_start or profile_end > clip_duration or
                    profile_end - profile_start < 0.1):
                raise ValueError("Choose a noise-only profile of at least 0.1 seconds inside the trimmed clip.")
            if not 0.1 <= strength <= 1.0:
                raise ValueError("Noise-reduction strength must be between 0.1 and 1.0.")
            samples = cropped[:, 0]
            nperseg = min(1024, len(samples))
            noverlap = nperseg * 3 // 4
            profile = samples[round(profile_start * sample_rate):round(profile_end * sample_rate)]
            if len(profile) < nperseg:
                raise ValueError("The noise profile is too short; select a clean-noise segment of at least 0.1 seconds.")
            _freq, _time, profile_stft = stft(profile, fs=sample_rate, window="hann", nperseg=nperseg,
                                               noverlap=noverlap, boundary=None, padded=False)
            _freq, _time, signal_stft = stft(samples, fs=sample_rate, window="hann", nperseg=nperseg,
                                               noverlap=noverlap, boundary="zeros", padded=True)
            noise_spectrum = np.percentile(np.abs(profile_stft), 60, axis=1)[:, None]
            magnitude = np.abs(signal_stft)
            residual = np.maximum(magnitude - strength * noise_spectrum, magnitude * 0.06)
            gain = residual / np.maximum(magnitude, 1e-9)
            gain = median_filter(gain, size=(3, 3), mode="nearest")
            _times, reduced = istft(signal_stft * gain, fs=sample_rate, window="hann", nperseg=nperseg,
                                    noverlap=noverlap, input_onesided=True, boundary=True)
            if len(reduced) < len(samples):
                reduced = np.pad(reduced, (0, len(samples) - len(reduced)))
            cropped = reduced[:len(samples)].astype(np.float32, copy=False)[:, None]
        if target_rate < 8_000 or target_rate > MAX_REFERENCE_RATE:
            raise ValueError("Reference target sample rate must be between 8 and 192 kHz.")
        if target_rate != sample_rate:
            common = math.gcd(target_rate, sample_rate)
            cropped = resample_poly(cropped, target_rate // common, sample_rate // common, axis=0).astype(np.float32)
        out_duration = cropped.shape[0] / target_rate
        if not MIN_REFERENCE_SECONDS <= out_duration <= maximum_seconds:
            raise ValueError(f"Selected reference must be {MIN_REFERENCE_SECONDS:g}–{MAX_REFERENCE_SECONDS:g} seconds; "
                             f"the {maximum_seconds:g}-second ceiling for this engine is enforced before GPU work.")
        peak = float(np.max(np.abs(cropped)))
        rms = float(np.sqrt(np.mean(np.square(cropped, dtype=np.float64))))
        if peak < 1e-6 or rms < 1e-3:
            raise ValueError("Reference audio is silent or too quiet (RMS below -60 dBFS). Record or select a louder passage.")
        clipped = int(np.count_nonzero(np.abs(cropped) >= 0.999))
        metrics = {
            "sample_rate": target_rate, "channels": 1, "duration_seconds": out_duration,
            "peak_dbfs": 20 * math.log10(max(peak, 1e-9)),
            "rms_dbfs": 20 * math.log10(max(rms, 1e-9)), "clipped_samples": clipped,
        }
        metrics["quality"] = reference_quality.assess(cropped[:, 0], target_rate, speakers=speakers)
        return np.ascontiguousarray(cropped, dtype=np.float32), metrics

    def preview_reference(self, identity_id, filename, source_bytes, *, edits=None):
        """Create a non-persistent processed reference preview using the selected identity's policy."""
        identity_id = _validate_id(identity_id, "identity id")
        identity = self._row("SELECT * FROM identities WHERE id=?", (identity_id,))
        if identity is None:
            raise KeyError("Voice identity not found.")
        if identity["kind"] != "reference":
            raise ValueError("Choose a reference-cloning identity before previewing a reference.")
        if edits is not None and not isinstance(edits, dict):
            raise ValueError("Reference edits must be an object.")
        audio, sample_rate, duration, _suffix = self._decode_audio(source_bytes, filename)
        policy = REFERENCE_POLICIES[identity["engine_id"]]
        processed, metrics = self._make_reference(audio, sample_rate, duration, edits or {}, policy["max_seconds"],
                                                  speakers=self.speakers)
        output = io.BytesIO()
        sf.write(output, processed, metrics["sample_rate"], format="WAV", subtype="PCM_16")
        return output.getvalue(), metrics

    def add_reference(self, identity_id, filename, source_bytes, *, transcript="", origin="", allowed_use="",
                      edits=None, parent_reference_id=None, parent_take_id=None):
        identity_id = _validate_id(identity_id, "identity id")
        identity = self._row("SELECT * FROM identities WHERE id=?", (identity_id,))
        if identity is None:
            raise KeyError("Voice identity not found.")
        if identity["kind"] != "reference":
            raise ValueError("This identity uses a fixed or preset checkpoint voice and does not accept reference audio.")
        if self.retirement_state("identity", identity_id):
            raise ValueError("This voice is retired; restore it before adding recordings.")
        filename = Path(str(filename or "reference.wav")).name
        if len(filename) > 180:
            raise ValueError("Reference filename must be 180 characters or fewer.")
        transcript = str(transcript or "").strip()
        origin, allowed_use = str(origin or "").strip(), str(allowed_use or "").strip()
        if len(transcript) > MAX_REFERENCE_TEXT:
            raise ValueError(f"Reference transcript must be {MAX_REFERENCE_TEXT} characters or fewer.")
        if len(origin) > 1000 or len(allowed_use) > 1000:
            raise ValueError("Reference origin and allowed-use notes must be 1000 characters or fewer.")
        if not origin or not allowed_use:
            raise ValueError("Record the reference origin and allowed-use terms before saving it.")
        edits = edits or {}
        audio, sample_rate, source_duration, suffix = self._decode_audio(source_bytes, filename)
        policy = REFERENCE_POLICIES[identity["engine_id"]]
        derived, metrics = self._make_reference(audio, sample_rate, source_duration, edits, policy["max_seconds"],
                                                speakers=self.speakers)
        if not transcript and identity["engine_id"] in TRANSCRIPT_ENGINES:
            raise ValueError(f"{identity['engine_id']} requires the exact transcript before saving this reference version.")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        audio_buf = io.BytesIO()
        sf.write(audio_buf, derived, metrics["sample_rate"], format="WAV", subtype="PCM_16")
        audio_bytes = audio_buf.getvalue()
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        ref_id = new_id()
        with self._connect() as db:
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM reference_versions WHERE identity_id=?",
                                 (identity_id,)).fetchone()[0]
        ref_dir = self.data_root / "media" / "references" / ref_id
        ref_dir.mkdir(parents=True, exist_ok=False)
        source_path = ref_dir / ("source" + suffix)
        audio_path = ref_dir / "reference.wav"
        try:
            with source_path.open("xb") as file:
                file.write(source_bytes)
            with audio_path.open("xb") as file:
                file.write(audio_bytes)
            stamp = now_utc()
            edit_record = {"start_seconds": float(edits.get("start_seconds", 0.0)),
                           "end_seconds": float(edits.get("end_seconds", source_duration)),
                           "target_sample_rate": metrics["sample_rate"],
                           "channel": edits.get("channel", "mono"), "source_duration_seconds": source_duration}
            with self._connect() as db:
                db.execute("""INSERT INTO reference_versions
                    (id,identity_id,version,source_name,source_path,audio_path,source_sha256,audio_sha256,transcript,
                     sample_rate,channels,duration_seconds,peak_dbfs,rms_dbfs,clipped_samples,origin,allowed_use,
                     parent_reference_id,parent_take_id,edit_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ref_id, identity_id, version, filename, self._relative(source_path), self._relative(audio_path),
                     source_hash, audio_hash, transcript, metrics["sample_rate"], metrics["channels"],
                     metrics["duration_seconds"], metrics["peak_dbfs"], metrics["rms_dbfs"], metrics["clipped_samples"],
                     origin, allowed_use, parent_reference_id, parent_take_id,
                     json.dumps(edit_record, ensure_ascii=False), stamp))
        except Exception:
            source_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            ref_dir.rmdir()
            raise
        return self._with_quality(ref_id, metrics)

    def _with_quality(self, reference_id, metrics):
        """The saved reference plus the pre-save quality screen of the audio it was saved from (not stored)."""
        saved = self.get_reference(reference_id)
        saved["quality"] = metrics["quality"]
        return saved

    def add_reference_version(self, reference_id, changes):
        reference_id = _validate_id(reference_id, "reference version id")
        previous = self._row("SELECT * FROM reference_versions WHERE id=?", (reference_id,))
        if previous is None:
            raise KeyError("Reference version not found.")
        if self.retirement_state("reference", reference_id) or self.retirement_state("identity", previous["identity_id"]):
            raise ValueError("This recording or its voice is retired; restore it before saving a new version.")
        identity = self._row("SELECT * FROM identities WHERE id=?", (previous["identity_id"],))
        source_path = self._media_path(previous["source_path"])
        source_bytes = source_path.read_bytes()
        transcript = str(changes.get("transcript", previous["transcript"]) or "").strip()
        if len(transcript) > MAX_REFERENCE_TEXT:
            raise ValueError(f"Reference transcript must be {MAX_REFERENCE_TEXT} characters or fewer.")
        edit = json.loads(previous["edit_json"])
        edit.update({key: changes[key] for key in ("start_seconds", "end_seconds", "target_sample_rate", "channel", "noise_reduction") if key in changes})
        origin = str(changes.get("origin", previous["origin"]) or "").strip()
        allowed_use = str(changes.get("allowed_use", previous["allowed_use"]) or "").strip()
        if not origin or not allowed_use:
            raise ValueError("Reference origin and allowed-use terms cannot be erased in a new version.")
        audio, sr, duration, _suffix = self._decode_audio(source_bytes, previous["source_name"])
        policy = REFERENCE_POLICIES[identity["engine_id"]]
        derived, metrics = self._make_reference(audio, sr, duration, edit, policy["max_seconds"], speakers=self.speakers)
        if not transcript and identity["engine_id"] in TRANSCRIPT_ENGINES:
            raise ValueError(f"{identity['engine_id']} requires the exact transcript before saving this reference version.")
        audio_buf = io.BytesIO()
        sf.write(audio_buf, derived, metrics["sample_rate"], format="WAV", subtype="PCM_16")
        audio_bytes = audio_buf.getvalue()
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        ref_id = new_id()
        with self._connect() as db:
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM reference_versions WHERE identity_id=?",
                                 (previous["identity_id"],)).fetchone()[0]
        ref_dir = self.data_root / "media" / "references" / ref_id
        ref_dir.mkdir(parents=True, exist_ok=False)
        audio_path = ref_dir / "reference.wav"
        try:
            with audio_path.open("xb") as file:
                file.write(audio_bytes)
            edit["source_duration_seconds"] = duration
            edit["target_sample_rate"] = metrics["sample_rate"]
            stamp = now_utc()
            with self._connect() as db:
                db.execute("""INSERT INTO reference_versions
                    (id,identity_id,version,source_name,source_path,audio_path,source_sha256,audio_sha256,transcript,
                     sample_rate,channels,duration_seconds,peak_dbfs,rms_dbfs,clipped_samples,origin,allowed_use,
                     parent_reference_id,parent_take_id,edit_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ref_id, previous["identity_id"], version, previous["source_name"], previous["source_path"],
                     self._relative(audio_path), previous["source_sha256"], audio_hash, transcript, metrics["sample_rate"],
                     metrics["channels"], metrics["duration_seconds"], metrics["peak_dbfs"], metrics["rms_dbfs"],
                     metrics["clipped_samples"], origin, allowed_use, reference_id, previous["parent_take_id"],
                     json.dumps(edit, ensure_ascii=False), stamp))
        except Exception:
            audio_path.unlink(missing_ok=True)
            ref_dir.rmdir()
            raise
        return self._with_quality(ref_id, metrics)

    def get_generation_identity(self, identity_id, reference_version_id=None):
        identity_id = _validate_id(identity_id, "identity id")
        row = self._row("SELECT * FROM identities WHERE id=?", (identity_id,))
        if row is None:
            raise KeyError("Voice identity not found.")
        if row["status"] == "rejected":
            raise ValueError("Rejected voice identities cannot be used for performance takes.")
        if self.retirement_state("identity", identity_id):
            raise ValueError("This voice is retired; restore it before generating performances.")
        identity = dict(row)
        identity["engine_options"] = json.loads(identity.pop("engine_options_json"))
        character = self._row("SELECT * FROM characters WHERE id=?", (row["character_id"],))
        identity["character_name"] = character["name"]
        reference = None
        if row["kind"] == "reference":
            reference_id = reference_version_id or row["approved_reference_id"]
            if not reference_id:
                raise ValueError("Choose a complete reference version to audition before generating performance takes.")
            reference = self.get_reference(reference_id)
            if reference["retired"]:
                raise ValueError("This reference recording is retired; choose another reference.")
            if reference["identity_id"] != identity_id:
                raise ValueError("Reference version must belong to the selected voice identity.")
            self._validate_reference(reference, row["engine_id"])
            reference["audio_path"] = str(self._media_path(reference["audio_path_relative"]))
        elif reference_version_id is not None:
            raise ValueError("This fixed/preset identity does not use a reference version.")
        identity["reference"] = reference
        return identity

    def add_reference_from_take(self, identity_id, take_id, *, transcript=None, edits=None, origin=None, allowed_use=None):
        identity_id = _validate_id(identity_id, "identity id")
        identity = self.get_identity(identity_id)
        if identity["kind"] != "reference":
            raise ValueError("Create a reference-cloning identity before deriving a reference from a take.")
        take_id = _validate_id(take_id, "take id")
        take = self.get_take(take_id)
        if take["retired"]:
            raise ValueError("This take is retired; new use is blocked.")
        source = self._media_path(take["audio_path"]).read_bytes()
        provenance = take["provenance"]
        inherited_use = provenance.get("allowed_use") or "Use subject to the local checkpoint license."
        requested_use = str(allowed_use or "").strip()
        if requested_use and requested_use != inherited_use:
            requested_use = f"{requested_use}; inherited restriction: {inherited_use}"
        else:
            requested_use = inherited_use
        derived_origin = str(origin or "").strip() or (
            f"Derived from local take {take_id} ({take['model'].get('backend', take['engine_id'])}, "
            f"{take['model'].get('checkpoint_revision', 'revision not reported')})."
        )
        parent_version_id = provenance.get("reference_version_id")
        if parent_version_id:
            parent = self.get_reference(parent_version_id)
            if parent["id"] != provenance.get("reference_version_id"):
                raise ValueError("The source take's reference provenance is inconsistent.")
            if parent["allowed_use"] not in requested_use:
                requested_use = f"{requested_use}; inherited restriction: {parent['allowed_use']}"
        return self.add_reference(
            identity_id, f"take-{take_id}.wav", source,
            transcript=transcript if transcript is not None else take.get("spoken_text", take["text"]),
            origin=derived_origin, allowed_use=requested_use, edits=edits,
            parent_reference_id=parent_version_id, parent_take_id=take_id,
        )

    def get_reference(self, reference_id):
        reference_id = _validate_id(reference_id, "reference version id")
        with self._connect() as db:
            row = db.execute("SELECT * FROM reference_versions WHERE id=?", (reference_id,)).fetchone()
            if row is None:
                raise KeyError("Reference version not found.")
            result = self._reference_dict(row)
            result.update(_retirement_fields(RetirementReader(db), "reference", reference_id))
        return result

    def _reference_dict(self, row):
        result = dict(row)
        result["edit"] = json.loads(result.pop("edit_json"))
        result["source_path_relative"] = result.pop("source_path")
        result["audio_path_relative"] = result.pop("audio_path")
        result["audio_url"] = f"/api/references/{result['id']}/audio"
        result["warnings"] = (["This reference contains clipped samples (>= -0.01 dBFS)." ]
                               if result["clipped_samples"] else [])
        return result

    def get_media_path(self, kind, item_id):
        item_id = _validate_id(item_id)
        if kind == "reference_source":
            table, column = "reference_versions", "source_path"
        else:
            table, column = (("reference_versions", "audio_path") if kind == "reference" else ("takes", "audio_path"))
        row = self._row(f"SELECT {column} FROM {table} WHERE id=?", (item_id,))
        if row is None:
            raise KeyError(f"{kind.title()} audio not found.")
        return self._media_path(row[0])

    def create_take(self, identity, *, line_id, text, spoken_text, language, direction, settings, model,
                    wav, sample_rate, generation_info, request_key=None, phoneme_timing=None):
        line_id = str(line_id or "").strip()
        text, spoken_text = str(text or ""), str(spoken_text or "")
        direction = str(direction or "")
        if not line_id or len(line_id) > 100:
            raise ValueError("Line ID must contain 1–100 characters.")
        if request_key is not None and (not isinstance(request_key, str) or not request_key or len(request_key) > 200):
            raise ValueError("Performance request key must contain 1–200 characters.")
        if not text.strip() or len(text) > 1200 or not spoken_text.strip() or len(spoken_text) > 1200:
            raise ValueError("Text sent to the model must contain 1–1200 characters.")
        audio = np.asarray(wav, dtype=np.float32).reshape(-1)
        if not audio.size or not np.isfinite(audio).all() or not int(sample_rate):
            raise ValueError("Generated performance audio is empty or invalid.")
        # Every take is labelled inside the file as AI-generated speech (synthetic_label.py).
        audio_bytes = synthetic_label.wav_bytes(audio, int(sample_rate), engine_id=identity["engine_id"])
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        take_id = new_id()
        take_dir = self.data_root / "media" / "takes" / take_id
        take_dir.mkdir(parents=True, exist_ok=False)
        audio_path = take_dir / "take.wav"
        try:
            with audio_path.open("xb") as file:
                file.write(audio_bytes)
            checks = _waveform_checks(audio_path, spoken_text)
            readings = _loudness_readings(audio_path)
            reference = identity.get("reference")
            if identity["kind"] == "preset_voice":
                options = identity["engine_options"]
                origin = (f"Preset speaker {options['speaker']} from "
                          f"{options['checkpoint']} revision {options['checkpoint_revision']}.")
                if options.get("voices_checkpoint"):
                    # The model and the voice catalog are separate repositories; the voice comes from the catalog.
                    origin = (f"Preset speaker {options['speaker']} from {options['voices_checkpoint']} revision "
                              f"{options['voices_revision']}, spoken by {options['checkpoint']} revision "
                              f"{options['checkpoint_revision']}.")
            elif identity["kind"] == "fixed_voice":
                origin = "Native/fixed voice selected in the local checkpoint."
            else:
                origin = reference["origin"]
            provenance = {
                "identity_id": identity["id"],
                "identity_type": identity["kind"],
                "identity_label": identity["label"],
                "engine_id": identity["engine_id"],
                "identity_options": identity.get("engine_options", {}),
                "origin": origin,
                "allowed_use": reference["allowed_use"] if reference else "Use subject to the local checkpoint license.",
                "reference_version_id": reference["id"] if reference else None,
                "source_sha256": reference["source_sha256"] if reference else None,
                "reference_sha256": reference["audio_sha256"] if reference else None,
                "parent_reference_id": reference["parent_reference_id"] if reference else None,
                "parent_take_id": reference["parent_take_id"] if reference else None,
            }
            stamp = now_utc()
            with self._connect() as db:
                db.execute("""INSERT INTO takes
                    (id,character_id,identity_id,reference_version_id,line_id,text,spoken_text,language,direction,
                     settings_json,model_json,provenance_json,audio_path,audio_sha256,sample_rate,duration_seconds,
                     status,rating,notes,created_at,request_key,waveform_checks_json,phoneme_timings_json,loudness_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (take_id, identity["character_id"], identity["id"], provenance["reference_version_id"], line_id,
                     text, spoken_text, str(language or ""), direction,
                     json.dumps(settings, ensure_ascii=False, sort_keys=True),
                     json.dumps(model, ensure_ascii=False, sort_keys=True),
                     json.dumps(provenance, ensure_ascii=False, sort_keys=True), self._relative(audio_path), audio_hash,
                     int(sample_rate), len(audio) / int(sample_rate), "candidate", None, "", stamp, request_key,
                     json.dumps(checks, ensure_ascii=False, sort_keys=True),
                     json.dumps(phoneme_timing, ensure_ascii=False, sort_keys=True) if phoneme_timing else "",
                     json.dumps(readings, sort_keys=True)))
        except sqlite3.IntegrityError:
            audio_path.unlink(missing_ok=True)
            take_dir.rmdir()
            if request_key is not None:
                existing = self.get_take_by_request_key(request_key)
                if existing is not None:
                    expected_reference = identity["reference"]["id"] if identity.get("reference") else None
                    if (existing["parent_take_id"] is not None or existing["identity_id"] != identity["id"] or
                            existing["reference_version_id"] != expected_reference or
                            existing["line_id"] != line_id or existing["text"] != text or
                            existing["spoken_text"] != spoken_text or existing["language"] != str(language or "") or
                            existing["direction"] != direction or existing["settings"] != settings):
                        raise ValueError("Performance idempotency_key was already used with different inputs.")
                    return existing
            raise
        except Exception:
            audio_path.unlink(missing_ok=True)
            take_dir.rmdir()
            raise
        return self.get_take(take_id)

    def get_take_by_request_key(self, request_key):
        if not isinstance(request_key, str) or not request_key:
            raise ValueError("Performance request key must be non-empty text.")
        row = self._row("SELECT id FROM takes WHERE request_key=?", (request_key,))
        return self.get_take(row[0]) if row else None

    def take_samples(self, take_id):
        """(the take, its mono 16-bit samples, its sample rate), read from the stored file once the file's checksum is the one
        the take was saved with: an edit never starts from audio that is not the take's own."""
        take = self.get_take(take_id)
        try:
            raw = self._media_path(take["audio_path"]).read_bytes()
        except FileNotFoundError:
            raise ValueError("This take's audio file is missing from the library, so it cannot be edited.") from None
        if hashlib.sha256(raw).hexdigest() != take["audio_sha256"]:
            raise ValueError("The stored audio no longer matches this take's checksum, so it cannot be edited.")
        samples, rate = sf.read(io.BytesIO(raw), dtype="int16", always_2d=False)
        return take, samples, int(rate)

    def take_waveform(self, take_id, points=600):
        """What a page needs to draw a take and to show where auto-trim would cut (take_edit.waveform)."""
        text = str(points)
        if not (text.isascii() and text.isdigit() and 10 <= int(text) <= 2000):
            raise ValueError("points must be a whole number from 10 to 2000.")
        _take, samples, rate = self.take_samples(take_id)
        return take_edit.waveform(samples, rate, int(text))

    def _edit_plan(self, take_id, request):
        parent = self.get_take(take_id)
        if parent["retired"]:
            raise ValueError("This take is retired; new use is blocked.")
        return parent, take_edit.normalize_edit(request, parent["duration_seconds"])

    def _render_edit(self, parent, edit):
        """(WAV bytes, the report of what the edit did, the sample rate) for an edit of the parent's audio."""
        _take, samples, rate = self.take_samples(parent["id"])
        edited, report = take_edit.apply_edit(samples, rate, edit)
        return synthetic_label.wav_bytes(edited, rate, engine_id=parent.get("engine_id")), report, rate

    def preview_edit(self, take_id, request):
        """The audio an edit would make, as WAV bytes, and the report of what it did. Nothing is saved."""
        parent, edit = self._edit_plan(take_id, request)
        audio_bytes, report, _rate = self._render_edit(parent, edit)
        return audio_bytes, report

    @staticmethod
    def _same_edit(existing, parent, edit):
        if existing["parent_take_id"] != parent["id"] or (existing["edit"] or {}).get("request") != edit:
            raise ValueError("Edit idempotency_key was already used with different inputs.")
        return existing

    def create_edited_take(self, parent_take_id, request, *, request_key=None):
        """Save an edit of a take as a new take. Returns (the new take, replayed).

        The parent is never changed. The new take has the parent's voice, line, texts, language, direction, settings, model and
        recording, a status of candidate, and `parent_take_id` and `edit` saying what it was made from. Its loudness and
        waveform checks are measured again on the new audio, and the parent's word and phoneme times move with the trim.
        With a request_key, the same edit of the same take again returns the take that was made, with replayed True."""
        if request_key is not None and (not isinstance(request_key, str) or not request_key or len(request_key) > 200):
            raise ValueError("The idempotency key must contain 1–200 characters.")
        parent, edit = self._edit_plan(parent_take_id, request)
        if request_key is not None:
            existing = self.get_take_by_request_key(request_key)
            if existing is not None:
                return self._same_edit(existing, parent, edit), True
        audio_bytes, report, rate = self._render_edit(parent, edit)
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        take_id = new_id()
        take_dir = self.data_root / "media" / "takes" / take_id
        take_dir.mkdir(parents=True, exist_ok=False)
        audio_path = take_dir / "take.wav"
        try:
            with audio_path.open("xb") as file:
                file.write(audio_bytes)
            checks = _waveform_checks(audio_path, parent["spoken_text"])
            readings = _loudness_readings(audio_path)
            with self._connect() as db:
                stored = db.execute("SELECT word_timings_json,phoneme_timings_json FROM takes WHERE id=?",
                                    (parent["id"],)).fetchone()
                window = (report["start_seconds"], report["end_seconds"], report["duration_seconds"])
                words = take_edit.derive_word_record(json.loads(stored[0]) if stored[0] else None, parent["id"], audio_hash, *window)
                phonemes = take_edit.derive_phoneme_record(json.loads(stored[1]) if stored[1] else None, parent["id"],
                                                           audio_hash, *window)
                provenance = {**parent["provenance"], "edited_from_take_id": parent["id"], "edit_request": edit}
                record = {"request": edit, "result": report, "parent_audio_sha256": parent["audio_sha256"],
                          "parent_duration_seconds": parent["duration_seconds"],
                          "word_times": "moved" if words else "none", "phoneme_times": "moved" if phonemes else "none"}
                db.execute("""INSERT INTO takes
                    (id,character_id,identity_id,reference_version_id,line_id,text,spoken_text,language,direction,
                     settings_json,model_json,provenance_json,audio_path,audio_sha256,sample_rate,duration_seconds,
                     status,rating,notes,created_at,request_key,waveform_checks_json,word_timings_json,
                     phoneme_timings_json,loudness_json,parent_take_id,edit_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (take_id, parent["character_id"], parent["identity_id"], parent["reference_version_id"], parent["line_id"],
                     parent["text"], parent["spoken_text"], parent["language"], parent["direction"],
                     json.dumps(parent["settings"], ensure_ascii=False, sort_keys=True),
                     json.dumps(parent["model"], ensure_ascii=False, sort_keys=True),
                     json.dumps(provenance, ensure_ascii=False, sort_keys=True), self._relative(audio_path), audio_hash,
                     rate, report["duration_seconds"], "candidate", None, "", now_utc(), request_key,
                     json.dumps(checks, ensure_ascii=False, sort_keys=True),
                     json.dumps(words, ensure_ascii=False, sort_keys=True) if words else "",
                     json.dumps(phonemes, ensure_ascii=False, sort_keys=True) if phonemes else "",
                     json.dumps(readings, sort_keys=True), parent["id"], json.dumps(record, ensure_ascii=False, sort_keys=True)))
        except sqlite3.IntegrityError:
            audio_path.unlink(missing_ok=True)
            take_dir.rmdir()
            if request_key is not None:
                existing = self.get_take_by_request_key(request_key)
                if existing is not None:
                    return self._same_edit(existing, parent, edit), True
            raise
        except Exception:
            audio_path.unlink(missing_ok=True)
            take_dir.rmdir()
            raise
        return self.get_take(take_id), False

    def get_take(self, take_id):
        take_id = _validate_id(take_id, "take id")
        with self._connect() as db:
            row = db.execute(f"{_TAKE_SELECT} WHERE t.id=?", (take_id,)).fetchone()
            if row is None:
                raise KeyError("Performance take not found.")
            return self._take_dict(row, RetirementReader(db))

    @staticmethod
    def _take_dict(row, reader):
        result = dict(row)
        for field, key in (("settings_json", "settings"), ("model_json", "model"), ("provenance_json", "provenance")):
            result[key] = json.loads(result.pop(field))
        result["waveform_checks"] = _stored_waveform_checks(result.pop("waveform_checks_json"))
        result["loudness"] = loudness.stored(result.pop("loudness_json", ""))
        # A flag, like the phoneme times: the record holds a probability for every word and no page reads it. It is true
        # for a take that was aligned and for one whose alignment failed; the exporter reads the record from the database.
        result["has_word_timings"] = bool(result.pop("word_timings_json", ""))
        result["has_phoneme_timings"] = bool(result.pop("phoneme_timings_json", ""))
        # An edited take names its parent (parent_take_id) and keeps the edit that made it; an original has neither.
        edit = result.pop("edit_json", "")
        result["edit"] = json.loads(edit) if edit else None
        result["audio_url"] = f"/api/takes/{result['id']}/audio"
        result.update(_retirement_fields(reader, "take", result["id"]))
        return result

    def get_takes(self, take_ids):
        """Full take records for the given IDs, in the order given. Unknown IDs are skipped."""
        ids = [_validate_id(item, "take id") for item in take_ids]
        found = {}
        with self._connect() as db:
            reader = RetirementReader(db)
            for chunk, marks in _in_chunks(ids):
                for row in db.execute(f"{_TAKE_SELECT} WHERE t.id IN ({marks})", chunk).fetchall():
                    found[row["id"]] = self._take_dict(row, reader)
        return [found[item] for item in ids if item in found]

    def list_takes(self, identity_id=None, line_id=None):
        clauses, args = [], []
        if identity_id:
            clauses.append("t.identity_id=?"); args.append(_validate_id(identity_id, "identity id"))
        if line_id:
            clauses.append("t.line_id=?"); args.append(str(line_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._rows("SELECT t.id FROM takes t" + where + " ORDER BY t.created_at,t.id", args)
        return self.get_takes([row[0] for row in rows])

    def list_takes_with_line_prefix(self, prefix):
        """Full take records whose line_id starts with this text, oldest first: a scene's takes, by its id's prefix."""
        prefix = str(prefix)
        rows = self._rows("SELECT id FROM takes WHERE substr(line_id,1,?)=? ORDER BY created_at,id", (len(prefix), prefix))
        return self.get_takes([row[0] for row in rows])

    def mark_takes_selected(self, take_ids):
        """Mark each take selected, as the one selected take of its identity and line, all in one transaction. Retired
        and unknown takes are left as they are."""
        ids = [_validate_id(item, "take id") for item in take_ids]
        with self._connect() as db:
            reader = RetirementReader(db)
            for take_id in ids:
                row = db.execute("SELECT identity_id,line_id FROM takes WHERE id=?", (take_id,)).fetchone()
                if row is None or reader.state("take", take_id) is not None:
                    continue
                db.execute("UPDATE takes SET status='candidate' WHERE identity_id=? AND line_id=? AND status='selected' "
                           "AND id<>?", (row[0], row[1], take_id))
                db.execute("UPDATE takes SET status='selected' WHERE id=?", (take_id,))

    def all_take_ids(self):
        return [row[0] for row in self._rows("SELECT id FROM takes ORDER BY created_at,id")]

    def take_ids_without_word_timings(self):
        """Takes that have no word-time record yet, oldest first; an aligner batch starts from this list."""
        return [row[0] for row in self._rows("SELECT id FROM takes WHERE word_timings_json='' ORDER BY created_at,id")]

    def set_word_timings(self, take_id, record):
        """Store a take's forced-alignment record. It derives from the take audio, which this never changes."""
        take_id = _validate_id(take_id, "take id")
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._connect() as db:
            db.execute("UPDATE takes SET word_timings_json=? WHERE id=?", (payload, take_id))

    def set_take_state(self, take_id, *, status=None, rating=None, notes=None):
        take_id = _validate_id(take_id, "take id")
        take = self.get_take(take_id)
        if take["retired"]:
            raise ValueError("This take is retired; restore it before changing its status.")
        status = status if status is not None else take["status"]
        if status not in ("candidate", "selected", "rejected"):
            raise ValueError("Take status must be candidate, selected, or rejected.")
        if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5):
            raise ValueError("Take rating must be an integer from 1 through 5.")
        notes = take["notes"] if notes is None else str(notes)
        if len(notes) > 2000:
            raise ValueError("Take notes must be 2000 characters or fewer.")
        with self._connect() as db:
            if status == "selected":
                db.execute("UPDATE takes SET status='candidate' WHERE identity_id=? AND line_id=? AND status='selected' AND id<>?",
                           (take["identity_id"], take["line_id"], take_id))
            db.execute("UPDATE takes SET status=?,rating=?,notes=? WHERE id=?", (status, rating, notes, take_id))
        return self.get_take(take_id)

    def _exists(self, db, kind, item_id):
        table = {"character": "characters", "identity": "identities", "reference": "reference_versions", "take": "takes"}[kind]
        return db.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,)).fetchone() is not None

    @staticmethod
    def _clear_retired_approvals(db):
        """An approved voice must point at a usable recording. Restoring never re-approves it."""
        reader = RetirementReader(db)
        for identity_id, reference_id in db.execute(
                "SELECT id,approved_reference_id FROM identities WHERE status='approved'").fetchall():
            if reader.state("identity", identity_id) or (reference_id and reader.state("reference", reference_id)):
                db.execute("UPDATE identities SET status='candidate',approved_reference_id=NULL,updated_at=? WHERE id=?",
                           (now_utc(), identity_id))

    def retirement_state(self, kind, item_id):
        """Retirement details for one item, or None while it is usable. Read-only."""
        kind, item_id = _retirement_target(kind, item_id)
        with self._connect() as db:
            return RetirementReader(db).state(kind, item_id)

    def retire(self, kind, item_id, *, reason=""):
        """Block new use of a character, voice, recording, or take, and everything made from it. Nothing is deleted."""
        kind, item_id = _retirement_target(kind, item_id)
        reason = str(reason or "").strip() or "Retired by the owner."
        if len(reason) > 300:
            raise ValueError("Retirement reason must be 300 characters or fewer.")
        with self._connect() as db:
            if not self._exists(db, kind, item_id):
                raise KeyError(f"{kind.capitalize()} not found.")
            active = db.execute("SELECT 1 FROM retirements WHERE target_kind=? AND target_id=? AND restored_at IS NULL",
                                (kind, item_id)).fetchone()
            if active is None:
                db.execute("INSERT INTO retirements(id,target_kind,target_id,reason,retired_at,restored_at) VALUES(?,?,?,?,?,NULL)",
                           (new_id(), kind, item_id, reason, now_utc()))
            self._clear_retired_approvals(db)
            affected = {name: len(ids) for name, ids in _dependents(db, kind, item_id).items()}
        return {"retirement": self.retirement_state(kind, item_id), "affected": affected}

    def restore(self, kind, item_id):
        """Lift this item's own retirement. Anything still retired through a source stays blocked."""
        kind, item_id = _retirement_target(kind, item_id)
        with self._connect() as db:
            active = db.execute("SELECT id FROM retirements WHERE target_kind=? AND target_id=? AND restored_at IS NULL",
                                (kind, item_id)).fetchone()
            if active is None:
                if RetirementReader(db).state(kind, item_id) is None:
                    raise ValueError("This item is not retired.")
                raise ValueError("This item is retired because something it came from is retired; restore that first.")
            db.execute("UPDATE retirements SET restored_at=? WHERE id=?", (now_utc(), active[0]))
            remaining = RetirementReader(db).state(kind, item_id)
            if remaining is not None:
                raise ValueError(f"It stays retired because {remaining['source_label']} is retired; restore that first.")
        return {"restored": True}

    def _media_plan(self, db, closure):
        refs = [dict(row) for row in _select_in(db, "SELECT * FROM reference_versions", "id", closure["reference"])]
        takes = [dict(row) for row in _select_in(db, "SELECT * FROM takes", "id", closure["take"])]
        files = []
        for row in refs:
            files += [(row["source_path"], row["source_sha256"]), (row["audio_path"], row["audio_sha256"])]
        files += [(row["audio_path"], row["audio_sha256"]) for row in takes]
        return refs, takes, files

    def _owned_media(self, relative):
        """A stored media path, provided it resolves inside this store's media folder."""
        path = _safe_relative(self.root, relative)
        if (self.data_root / "media").resolve() not in path.parents:
            raise ValueError("Refusing to change a file outside Cast media storage.")
        return path

    def _external_copies(self, files):
        """Derived copies under out/ that match a purged file byte for byte (same size, then SHA-256)."""
        wanted = {}
        for relative, digest in files:
            if not digest:
                continue
            try:
                size = self._owned_media(relative).stat().st_size
            except (OSError, ValueError):
                continue
            wanted.setdefault(size, set()).add(digest)
        found, out = [], self.root / "out"
        if not wanted or not out.is_dir():
            return found, False
        for directory, _folders, names in os.walk(out):
            for name in names:
                path = Path(directory) / name
                if path.suffix.lower() not in AUDIO_SUFFIXES:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size in wanted and _sha256_file(path) in wanted[size]:
                    found.append(path.relative_to(self.root).as_posix())
                    if len(found) >= MAX_EXTERNAL_REPORT:
                        return found, True
        return found, False

    def purge_preview(self, kind, item_id):
        """What a purge would remove, and which copies outside Cast storage would still hold the audio."""
        kind, item_id = _retirement_target(kind, item_id)
        with self._connect() as db:
            if not self._exists(db, kind, item_id):
                raise KeyError(f"{kind.capitalize()} not found.")
            state = RetirementReader(db).state(kind, item_id)
            if state is None:
                raise ValueError("Retire this voice, recording, or take before purging it.")
            closure = _dependents(db, kind, item_id)
            _refs, _takes, files = self._media_plan(db, closure)
            external, truncated = self._external_copies(files)
        return {"root": {"kind": kind, "id": item_id}, "retirement": state,
                "would_purge": {name: len(ids) for name, ids in closure.items()},
                "media_files": len(files), "external_copies": external, "external_copies_truncated": truncated}

    def purge(self, kind, item_id, *, reason="", confirm=False):
        """Permanently delete a retired item, everything made from it, and their audio.

        Files go first. If one cannot be removed, nothing is purged from the database, so the
        purge can be retried. Copies outside Cast storage are listed for the owner, never deleted.
        """
        if confirm is not True:
            raise ValueError("Purging permanently deletes audio and records. Confirm it to continue.")
        kind, item_id = _retirement_target(kind, item_id)
        reason = str(reason or "").strip()[:300] or "Purged by the owner."
        with self._connect() as db:
            if not self._exists(db, kind, item_id):
                raise KeyError(f"{kind.capitalize()} not found.")
            if RetirementReader(db).state(kind, item_id) is None:
                raise ValueError("Retire this voice, recording, or take before purging it.")
            closure = _dependents(db, kind, item_id)
            refs, takes, files = self._media_plan(db, closure)
            external, truncated = self._external_copies(files)
            for relative, _digest in files:
                try:
                    self._owned_media(relative).unlink(missing_ok=True)
                except OSError as exc:
                    raise ValueError(f"Could not remove {relative}: {exc}. Nothing was purged from the database; "
                                     "close any program using that file, then purge again.") from exc
            media = self.data_root / "media"
            for folder in [media / "references" / row["id"] for row in refs] + [media / "takes" / row["id"] for row in takes]:
                try:
                    folder.rmdir()
                except OSError:
                    pass  # Already gone, or not empty: nothing more to tidy.
            batch_id, stamp = new_id(), now_utc()
            counts = {name: len(ids) for name, ids in closure.items()}
            if closure["reference"]:
                marks = ",".join("?" * len(closure["reference"]))
                db.execute(f"UPDATE identities SET status='candidate',approved_reference_id=NULL,updated_at=? "
                           f"WHERE approved_reference_id IN ({marks})", (stamp, *sorted(closure["reference"])))
            _delete_in(db, "takes", "id", closure["take"])
            pending = {row["id"]: row["parent_reference_id"] for row in refs}
            while pending:
                parents = {value for value in pending.values() if value}
                leaves = [item for item in pending if item not in parents]
                if not leaves:
                    raise RuntimeError("Reference versions form a cycle; nothing further was purged.")
                for item in leaves:
                    db.execute("DELETE FROM reference_versions WHERE id=?", (item,))
                    del pending[item]
            _delete_in(db, "identities", "id", closure["identity"])
            _delete_in(db, "pronunciations", "character_id", closure["character"])
            _delete_in(db, "characters", "id", closure["character"])
            digests = {("reference", row["id"]): [value for value in (row["source_sha256"], row["audio_sha256"]) if value]
                       for row in refs}
            digests.update({("take", row["id"]): [row["audio_sha256"]] for row in takes})
            db.execute("INSERT INTO purge_batches(id,root_kind,root_id,reason,purged_at,counts_json,external_copies_json) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (batch_id, kind, item_id, reason, stamp, json.dumps(counts), json.dumps(external, ensure_ascii=False)))
            for target_kind, ids in closure.items():
                for target_id in sorted(ids):
                    db.execute("INSERT INTO purged_items(id,batch_id,target_kind,target_id,sha256s_json) VALUES(?,?,?,?,?)",
                               (new_id(), batch_id, target_kind, target_id,
                                json.dumps(digests.get((target_kind, target_id), []))))
        return {"batch_id": batch_id, "root": {"kind": kind, "id": item_id}, "purged": counts,
                "media_files": len(files), "external_copies": external, "external_copies_truncated": truncated}

    def assert_media_usable(self, path):
        """Refuse new use of a stored recording whose voice, source recording, or take is retired."""
        try:
            relative = Path(path).resolve().relative_to(self.root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return  # Not Cast storage, so Cast retirement does not apply.
        with self._connect() as db:
            reader = RetirementReader(db)
            for (reference_id,) in db.execute(
                    "SELECT id FROM reference_versions WHERE audio_path=? OR source_path=?", (relative, relative)).fetchall():
                if reader.state("reference", reference_id):
                    raise ValueError("This reference recording is retired; new use is blocked.")
            for (take_id,) in db.execute("SELECT id FROM takes WHERE audio_path=?", (relative,)).fetchall():
                if reader.state("take", take_id):
                    raise ValueError("This take is retired; new use is blocked.")

    def assert_identity_usable(self, identity_id, reference_version_id=None):
        """Raise when a voice, or the recording chosen for it, is retired. Used before queueing batch rows."""
        if self.retirement_state("identity", identity_id):
            raise ValueError("This voice is retired; restore it before rendering.")
        if reference_version_id and self.retirement_state("reference", reference_version_id):
            raise ValueError("This reference recording is retired; choose another reference before rendering.")

    def take_summaries_for(self, take_ids):
        """Checks, loudness, retirement, and purge state for many takes, read in batches for scene rows."""
        ids = [_validate_id(item, "take id") for item in take_ids]
        found = {}
        with self._connect() as db:
            reader = RetirementReader(db)
            for chunk, marks in _in_chunks(ids):
                for row in db.execute(f"SELECT id,waveform_checks_json,loudness_json FROM takes WHERE id IN ({marks})",
                                      chunk).fetchall():
                    found[row[0]] = {"waveform_checks": _stored_waveform_checks(row[1]), "loudness": loudness.stored(row[2]),
                                     "retirement": reader.state("take", row[0]), "purged": False}
                for (purged_id,) in db.execute(
                        f"SELECT target_id FROM purged_items WHERE target_kind='take' AND target_id IN ({marks})", chunk).fetchall():
                    found.setdefault(purged_id, {"waveform_checks": None, "loudness": None, "retirement": None, "purged": True})
        return found

    def measure_missing_loudness(self):
        """Read the audio of every take that has no loudness record and store one. A take whose audio cannot be read
        gets an unavailable record, so it is not tried again. Returns how many were measured and how many failed."""
        pending = self._rows("SELECT id,audio_path FROM takes WHERE loudness_json='' ORDER BY created_at,id")
        measured = failed = 0
        for take_id, relative in pending:
            try:
                readings = loudness.measure_file(self._media_path(relative))
            except Exception as exc:
                readings, failed = loudness.unavailable(exc), failed + 1
            else:
                measured += 1
            with self._connect() as db:
                # Only a take that is still unmeasured: a take saved meanwhile already carries its own record.
                db.execute("UPDATE takes SET loudness_json=? WHERE id=? AND loudness_json=''",
                           (json.dumps(readings, sort_keys=True), take_id))
        return {"measured": measured, "failed": failed}

    def list_cast(self):
        with self._connect() as db:
            reader = RetirementReader(db)
            result = []
            for char in db.execute("SELECT * FROM characters ORDER BY name COLLATE NOCASE,id").fetchall():
                item = dict(char)
                item["tags"] = json.loads(item.pop("tags_json"))
                item.update(_retirement_fields(reader, "character", item["id"]))
                item["identities"] = []
                for identity in db.execute("SELECT * FROM identities WHERE character_id=? ORDER BY engine_id,label COLLATE NOCASE",
                                           (char["id"],)).fetchall():
                    ident = dict(identity)
                    ident["engine_options"] = json.loads(ident.pop("engine_options_json"))
                    ident.update(_retirement_fields(reader, "identity", ident["id"]))
                    ident["references"] = []
                    for ref in db.execute("SELECT * FROM reference_versions WHERE identity_id=? ORDER BY version DESC",
                                          (identity["id"],)).fetchall():
                        reference = self._reference_dict(ref)
                        reference.update(_retirement_fields(reader, "reference", reference["id"]))
                        ident["references"].append(reference)
                    item["identities"].append(ident)
                result.append(item)
        return result

    def get_character(self, char_id):
        char_id = _validate_id(char_id, "character id")
        for char in self.list_cast():
            if char["id"] == char_id:
                return char
        raise KeyError("Character not found.")

    def get_identity(self, identity_id):
        identity_id = _validate_id(identity_id, "identity id")
        for char in self.list_cast():
            for identity in char["identities"]:
                if identity["id"] == identity_id:
                    identity["character_name"] = char["name"]
                    return identity
        raise KeyError("Voice identity not found.")

    def list_pronunciations(self, character_id=None):
        if character_id:
            character_id = _validate_id(character_id, "character id")
            rows = self._rows("SELECT * FROM pronunciations WHERE character_id IS NULL OR character_id=? "
                              "ORDER BY length(source) DESC,source COLLATE NOCASE", (character_id,))
        else:
            rows = self._rows("SELECT * FROM pronunciations WHERE character_id IS NULL ORDER BY length(source) DESC,source COLLATE NOCASE")
        return [dict(row) for row in rows]

    def save_pronunciation(self, *, source, replacement, character_id=None, notes=""):
        source, replacement, notes = str(source or "").strip(), str(replacement or "").strip(), str(notes or "")
        if not source or not replacement or len(source) > 100 or len(replacement) > 200 or len(notes) > 500:
            raise ValueError("Pronunciation entries need a 1–100 character term, a 1–200 character spoken form, and short notes.")
        if character_id:
            character_id = _validate_id(character_id, "character id")
            if self._row("SELECT id FROM characters WHERE id=?", (character_id,)) is None:
                raise KeyError("Character not found.")
        stamp = now_utc()
        existing = self._row("SELECT id FROM pronunciations WHERE character_id IS ? AND source=? COLLATE NOCASE",
                             (character_id, source))
        pronunciation_id = existing[0] if existing else new_id()
        with self._connect() as db:
            if existing:
                db.execute("UPDATE pronunciations SET replacement=?,notes=?,updated_at=? WHERE id=?",
                           (replacement, notes, stamp, pronunciation_id))
            else:
                db.execute("INSERT INTO pronunciations VALUES(?,?,?,?,?,?,?)",
                           (pronunciation_id, character_id, source, replacement, notes, stamp, stamp))
        return next(item for item in self.list_pronunciations(character_id) if item["id"] == pronunciation_id)

    def apply_pronunciations(self, text, character_id=None, entries=None):
        """The text as the model should read it, with this character's pronunciations (and everyone's) put in. `entries`,
        when given, is list_pronunciations(character_id) already read, for a caller that applies it many times."""
        import re
        result = text
        for item in self.list_pronunciations(character_id) if entries is None else entries:
            pattern = re.compile(r"(?<!\w)" + re.escape(item["source"]) + r"(?!\w)", re.IGNORECASE)
            result = pattern.sub(lambda _match, value=item["replacement"]: value, result)
        return result
