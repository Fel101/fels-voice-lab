"""Versioned scene manifests and crash-resumable sequential performance batches."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import resample_poly

import synthetic_label
from cast_store import ID_RE, CastStore, DIRECTION_ENGINES, _validate_id, generation_identity_snapshot
import engine_adapters
from db_snapshots import snapshot_before_migration
from local_ownership import LocalOwnership
from performance_settings import effective_settings
from scene_selection import take_problem, validate_selected_take
from script_import import parse_script


SCHEMA_VERSION = 1
MAX_SCENE_LINES = 500
MAX_LINE_TEXT = 10_000
MAX_SEGMENT_TEXT = 1200
MAX_PAUSE_MS = 10_000
MAX_TARGET_MS = 120_000
SCENE_ID_RE = re.compile(r"\A[a-f0-9]{32}\Z")
LINE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
SUPPORTED_LANGUAGES = {
    # The engines whose adapters say (engine_adapters/).
    **{engine: set(languages) for engine, languages in engine_adapters.table("languages").items()},
}
CSV_FIELDS = [
    "schema_version", "scene_name", "line_id", "line_type", "character_id", "identity_id",
    "engine_id", "reference_version_id", "text", "language", "direction", "pause_before_ms", "pause_after_ms",
    "take_count", "selected_take_id", "selected_takes_json", "target_duration_ms", "settings_json",
    "use_pronunciations",
]
# What changed after a take was made, in the words the scenes table shows (scene_selection.take_problem names the part).
STALE_REASONS = {
    "segments": "The text changed after this take was made.",
    "text": "The text changed after this take was made.",
    "line_id": "This take was made for another line.",
    "identity_id": "The voice changed after this take was made.",
    "character_id": "The character changed after this take was made.",
    "reference_version_id": "The voice recording changed after this take was made.",
    "voice_identity": "The voice's settings changed after this take was made.",
    "direction": "The acting direction changed after this take was made.",
    "rejected": "This take was rejected.",
    "language": "The language changed after this take was made.",
    "pronunciations": "Pronunciations were turned on or off after this take was made.",
    "settings": "The model settings changed after this take was made.",
    "spoken_text": "The pronunciation dictionary changed after this take was made.",
}
# A line has the status of its segments that comes first here.
LINE_STATUS_ORDER = ("rendering", "waiting", "failed", "out_of_date", "not_rendered", "choose_take", "done")
ACTIVE_JOB_STATUSES = ("queued", "running", "paused")

_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    key = str(Path(path).resolve()).casefold()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


def batch_models(rows, entries):
    """The models a batch renders, in the order it renders them: [{engine_id, name, total, done}], `done` counting finished,
    failed and cancelled rows. create_job sorts a batch's rows by model, then reference, then script order, so each model
    renders all of its lines before the next one loads; a model whose rows are apart (none are, as create_job makes them)
    would show twice."""
    parts = []
    for item in rows:
        engine = item.get("engine_id")
        if not parts or parts[-1]["engine_id"] != engine:
            name = (entries.get(engine) or {}).get("name") or engine or "An unknown model"
            parts.append({"engine_id": engine, "name": name, "total": 0, "done": 0})
        parts[-1]["total"] += 1
        parts[-1]["done"] += item["status"] in ("completed", "failed", "cancelled")
    return parts


def _chosen_take_ids(lines):
    """The take ids a scene's lines name as chosen, once each, leaving out anything that is not a take id."""
    found = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        chosen = line.get("selected_takes")
        found += [line.get("selected_take_id"), *(chosen.values() if isinstance(chosen, dict) else ())]
    return list(dict.fromkeys(item for item in found if isinstance(item, str) and ID_RE.fullmatch(item)))


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} through {high}.")
    return value


def _sentence_chunks(text):
    """Split at language-neutral sentence marks without cutting balanced inline spans."""
    protected = {"Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "Sr.", "Jr.", "St.", "vs.", "etc.",
                 "e.g.", "i.e.", "a.m.", "p.m.", "U.S.", "U.K."}
    boundaries = []
    stack = []
    for index, char in enumerate(text):
        if char in "[{<":
            stack.append({"[": "]", "{": "}", "<": ">"}[char])
        elif stack and char == stack[-1]:
            stack.pop()
        if stack or char not in ".!?。？！؟؛":
            continue
        if char == ".":
            if index > 0 and index + 1 < len(text) and text[index - 1].isdigit() and text[index + 1].isdigit():
                continue
            start_word = max(text.rfind(" ", 0, index), text.rfind("\n", 0, index)) + 1
            preceding = text[start_word:index + 1]
            if any(preceding.endswith(abbreviation) for abbreviation in protected):
                continue
            # Initials and dotted initialisms are not sentence boundaries.
            if re.search(r"(?:\b[A-Z]|\b(?:[A-Z]\.){2,})\.$", preceding):
                continue
        end = index + 1
        while end < len(text) and text[end] in '"\'”’)]}>' :
            end += 1
        boundaries.append(end)
    pieces = []
    cursor = 0
    for end in boundaries:
        piece = text[cursor:end].strip()
        if piece:
            pieces.append(piece)
        cursor = end
    tail = text[cursor:].strip()
    if tail:
        pieces.append(tail)
    return pieces or [text.strip()]


def segment_text(text, limit=MAX_SEGMENT_TEXT, protected_phrases=()):
    """Keep sentences/tags together where possible; split very long spans at safe spaces."""
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    sentences = _sentence_chunks(text)
    chunks, current = [], ""
    for sentence in sentences:
        if len(sentence) > limit:
            phrases = sorted({str(phrase).strip() for phrase in protected_phrases if str(phrase).strip()},
                             key=len, reverse=True)
            phrase_pattern = "|".join(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)" for phrase in phrases)
            markup_pattern = r"<([A-Za-z][\w:-]*)\b[^>]*>.*?</\1>"
            token_pattern = r"\[[^\]]*\]|\{[^}]*\}|" + markup_pattern + "|"
            if phrase_pattern:
                token_pattern += phrase_pattern + "|"
            token_pattern += r"\S+\s*"
            tokens = [match.group(0) for match in re.finditer(token_pattern, sentence,
                       flags=re.IGNORECASE | re.DOTALL)]
            if not tokens:
                tokens = [sentence[i:i + limit] for i in range(0, len(sentence), limit)]
            for token in tokens:
                token = token.strip()
                if not token:
                    continue
                if len(token) > limit:
                    # An individual unbroken token/tag cannot be split without corrupting it.
                    raise ValueError("A text token or inline tag exceeds the 1200-character segment limit.")
                if current and len(current) + 1 + len(token) > limit:
                    chunks.append(current)
                    current = ""
                current = f"{current} {token}".strip()
            continue
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    if any(len(chunk) > limit for chunk in chunks):
        raise AssertionError("Text segmenter produced an oversized chunk.")
    return chunks


class SceneStore:
    """SQLite job state with checksummed, human-readable JSON scene snapshots."""

    def __init__(self, cast: CastStore, engine_entries=None, *, root=None):
        self.cast = cast
        self.root = Path(root).resolve() if root is not None else cast.root
        self.data_root = self.root / "data" / "scenes"
        self.db_path = self.data_root / "scenes.sqlite3"
        self.manifest_root = self.data_root / "manifests"
        self.output_root = self.root / "out" / "scenes"
        self.entries = engine_entries or {}
        self._lock = threading.RLock()
        self._init_db()
        self._repair_manifest_files()

    @contextmanager
    def _connect(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_db(self):
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Scene database schema {version} is newer than this voice-lab build.")
            snapshot_before_migration(db, self.db_path, version, SCHEMA_VERSION, "scenes")
            if version == 0:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE scenes (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        manifest_json TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY,
                        scene_id TEXT NOT NULL REFERENCES scenes(id),
                        scene_revision INTEGER NOT NULL,
                        scene_checksum TEXT NOT NULL,
                        scene_snapshot_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('queued','running','paused','completed','failed','cancelled')),
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        pause_requested INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE job_rows (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL,
                        scene_order INTEGER NOT NULL,
                        line_id TEXT NOT NULL,
                        segment_id TEXT NOT NULL,
                        segment_index INTEGER NOT NULL,
                        take_index INTEGER NOT NULL,
                        cast_line_id TEXT NOT NULL,
                        identity_id TEXT NOT NULL,
                        reference_version_id TEXT,
                        text TEXT NOT NULL,
                        language TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        settings_json TEXT NOT NULL,
                        use_pronunciations INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
                        request_key TEXT NOT NULL UNIQUE,
                        take_id TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(job_id, line_id, segment_index, take_index)
                    );
                    CREATE INDEX job_queue ON jobs(status, created_at);
                    CREATE INDEX job_row_queue ON job_rows(job_id, status, ordinal);
                    PRAGMA user_version=1;
                    COMMIT;
                """)

    @property
    def ownership_path(self):
        return self.root / "out" / "runtime-locks" / "scene-batch.lock"

    def recover_interrupted_jobs(self, ownership=None):
        """Only the exclusive runner may classify running rows as abandoned."""
        owned_here = ownership is None
        lease = ownership or LocalOwnership(self.ownership_path)
        if lease.path != self.ownership_path.resolve():
            raise ValueError("Batch ownership belongs to a different workspace.")
        if owned_here:
            if not lease.acquire():
                return []
        elif not lease.held:
            raise ValueError("Batch recovery requires active exclusive ownership.")
        try:
            return self._recover_abandoned_jobs()
        finally:
            if owned_here:
                lease.release()

    def _recover_abandoned_jobs(self):
        stamp = utc_now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            running = [row[0] for row in db.execute("SELECT id FROM jobs WHERE status='running'")]
            if running:
                for job_id in running:
                    flags = db.execute("SELECT cancel_requested,pause_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
                    state = "cancelled" if flags[0] else "paused" if flags[1] else "queued"
                    row_state = "cancelled" if flags[0] else "queued"
                    db.execute("UPDATE job_rows SET status=?,updated_at=? WHERE job_id=? AND status IN ('running','queued')",
                               (row_state, stamp, job_id))
                    db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (state, stamp, job_id))
        for job_id in running:
            self._write_job_manifest(job_id)
        return running

    def _manifest_path(self, scene_id):
        return self.manifest_root / f"{scene_id}.json"

    def _atomic_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        with _path_lock(path):
            try:
                with temp.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                for attempt in range(6):
                    try:
                        temp.replace(path)
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.025 * (attempt + 1))
            finally:
                temp.unlink(missing_ok=True)

    def _repair_manifest_files(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,manifest_json FROM scenes").fetchall()
        for row in rows:
            target = self._manifest_path(row["id"])
            expected = (json.dumps(json.loads(row["manifest_json"]), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if not target.is_file() or target.read_bytes() != expected:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
                try:
                    temp.write_bytes(expected)
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)

    def _normalize_scene(self, payload, scene_id, revision, previous=None):
        if not isinstance(payload, dict):
            raise ValueError("Scene manifest must be a JSON object.")
        schema = payload.get("schema_version", SCHEMA_VERSION)
        if schema != SCHEMA_VERSION:
            raise ValueError(f"Unsupported scene schema version {schema!r}; expected {SCHEMA_VERSION}.")
        name = str(payload.get("name", "Untitled scene") or "").strip()
        if not name or len(name) > 120:
            raise ValueError("Scene name must contain 1–120 characters.")
        source_lines = payload.get("lines")
        if not isinstance(source_lines, list) or not source_lines or len(source_lines) > MAX_SCENE_LINES:
            raise ValueError(f"A scene must contain 1–{MAX_SCENE_LINES} ordered lines.")
        normalized, seen = [], set()
        # A scene's lines share a few voices and characters: read each once per save, not once per line.
        identities, pronunciation_lists = {}, {}
        known_takes = {take["id"]: take for take in self.cast.get_takes(_chosen_take_ids(source_lines))}
        for index, source in enumerate(source_lines):
            if not isinstance(source, dict):
                raise ValueError(f"Scene line {index + 1} must be an object.")
            line_id = source.get("line_id", source.get("id"))
            if not isinstance(line_id, str) or not LINE_ID_RE.fullmatch(line_id):
                raise ValueError(f"Scene line {index + 1} needs a stable ID using letters, digits, dot, underscore, colon, or hyphen (1–64 characters).")
            if line_id in seen:
                raise ValueError(f"Duplicate scene line ID {line_id!r}.")
            seen.add(line_id)
            kind = source.get("line_type", source.get("kind", "dialogue"))
            if kind not in ("dialogue", "narration"):
                raise ValueError(f"Line {line_id}: line_type must be dialogue or narration.")
            text = source.get("text")
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_LINE_TEXT:
                raise ValueError(f"Line {line_id}: text must contain 1–{MAX_LINE_TEXT} characters.")
            text = text.strip()
            identity_id = source.get("identity_id")
            try:
                identity_key = canonical_json([identity_id, source.get("reference_version_id")])
                if identity_key not in identities:
                    identities[identity_key] = self.cast.get_generation_identity(identity_id, source.get("reference_version_id"))
                identity = identities[identity_key]
            except (ValueError, KeyError) as exc:
                message = str(exc)
                if "not found" in message.lower():
                    raise ValueError(f"Line {line_id}: missing character or voice identity: {message}") from exc
                raise ValueError(f"Line {line_id}: {message}") from exc
            character_id = source.get("character_id") or identity["character_id"]
            if character_id != identity["character_id"]:
                raise ValueError(f"Line {line_id}: selected voice identity belongs to a different character.")
            engine_id = identity["engine_id"]
            if source.get("engine_id") not in (None, "", engine_id):
                raise ValueError(f"Line {line_id}: engine_id does not match the selected voice identity.")
            identity_options = identity.get("engine_options") or {}
            default_language = identity_options.get("language", "English") if identity["kind"] == "preset_voice" else "English"
            language = source.get("language", default_language)
            allowed = (set(identity_options.get("supported_languages") or [default_language])
                       if identity["kind"] == "preset_voice" else SUPPORTED_LANGUAGES.get(engine_id, {"English"}))
            language_match = next((value for value in allowed if isinstance(language, str) and value.casefold() == language.casefold()), None)
            if language_match is None:
                raise ValueError(f"Line {line_id}: {engine_id} does not support language {language!r}; supported values: {', '.join(sorted(allowed))}.")
            language = language_match
            use_pronunciations = source.get("use_pronunciations", False)
            if not isinstance(use_pronunciations, bool):
                raise ValueError(f"Line {line_id}: use_pronunciations must be a boolean.")
            if use_pronunciations and character_id not in pronunciation_lists:
                pronunciation_lists[character_id] = self.cast.list_pronunciations(character_id)
            pronunciations = pronunciation_lists[character_id] if use_pronunciations else []
            pronunciation_sources = [item["source"] for item in pronunciations]
            segment_limit = MAX_SEGMENT_TEXT
            while True:
                segments = segment_text(text, segment_limit, pronunciation_sources)
                if not use_pronunciations:
                    break
                expanded_lengths = [len(self.cast.apply_pronunciations(part, character_id, pronunciations)) for part in segments]
                overflow = max(expanded_lengths, default=0) - MAX_SEGMENT_TEXT
                if overflow <= 0:
                    break
                minimum = max([len(item) for item in pronunciation_sources] + [1])
                next_limit = max(minimum, segment_limit - max(overflow, 1))
                if next_limit == segment_limit:
                    raise ValueError(f"Line {line_id}: a pronunciation replacement expands beyond the 1200-character segment limit.")
                segment_limit = next_limit
            direction = source.get("direction", "")
            if not isinstance(direction, str) or len(direction) > 1000:
                raise ValueError(f"Line {line_id}: direction must be text of 1000 characters or fewer.")
            if direction.strip() and engine_id not in DIRECTION_ENGINES:
                raise ValueError(f"Line {line_id}: {engine_id} has no verified text-direction control.")
            pause_before = _integer(source.get("pause_before_ms", 0), f"Line {line_id} pause_before_ms", 0, MAX_PAUSE_MS)
            pause_after = _integer(source.get("pause_after_ms", 0), f"Line {line_id} pause_after_ms", 0, MAX_PAUSE_MS)
            take_count = _integer(source.get("take_count", 1), f"Line {line_id} take_count", 1, 10)
            target_duration = source.get("target_duration_ms")
            if target_duration is not None:
                target_duration = _integer(target_duration, f"Line {line_id} target_duration_ms", 1, MAX_TARGET_MS)
            settings = source.get("settings", {})
            if not isinstance(settings, dict) or len(canonical_json(settings)) > 4096:
                raise ValueError(f"Line {line_id}: settings must be an object of at most 4096 serialized characters.")
            effective_settings(engine_id, settings)
            segments_value = [{"id": f"seg-{part + 1:03d}", "text": piece} for part, piece in enumerate(segments)]
            voice_identity = generation_identity_snapshot(identity)
            candidate_line = {"line_id": line_id, "text": text, "identity_id": identity_id, "character_id": character_id,
                "engine_id": engine_id, "reference_version_id": identity["reference"]["id"] if identity["reference"] else None,
                "voice_identity": voice_identity, "language": language, "direction": direction, "settings": settings,
                "use_pronunciations": use_pronunciations, "segments": segments_value}
            old_line = next((item for item in (previous or {}).get("lines", []) if item["line_id"] == line_id), {})
            old_selected = old_line.get("selected_takes", {})
            selected_take_id = source.get("selected_take_id")
            selected_takes = source.get("selected_takes", {})
            if selected_take_id is not None:
                _validate_id(selected_take_id, "selected take id")
                selected_takes = dict(selected_takes or {})
                if len(segments_value) == 1:
                    selected_takes.setdefault(segments_value[0]["id"], selected_take_id)
            if not isinstance(selected_takes, dict):
                raise ValueError(f"Line {line_id}: selected_takes must map segment IDs to take IDs.")
            checked_selected = {}
            for segment_id, take_id in selected_takes.items():
                if segment_id not in {part["id"] for part in segments_value}:
                    if old_selected.get(segment_id) == take_id:
                        continue
                    raise ValueError(f"Line {line_id}: selected take refers to unknown segment {segment_id!r}.")
                _validate_id(take_id, "selected take id")
                try:
                    selected_take = known_takes.get(take_id) or self.cast.get_take(take_id)
                except KeyError as exc:
                    raise ValueError(f"Line {line_id}: selected take {take_id} does not exist.") from exc
                if selected_take["retired"] and old_selected.get(segment_id) != take_id:
                    raise ValueError(f"Line {line_id}: selected take {take_id} is retired; choose another take.")
                segment = next(item for item in segments_value if item["id"] == segment_id)
                spoken = (self.cast.apply_pronunciations(segment["text"], character_id, pronunciations)
                          if use_pronunciations else segment["text"])
                try:
                    validate_selected_take(scene_id, candidate_line, segment, selected_take, spoken)
                except ValueError:
                    if old_selected.get(segment_id) == take_id:
                        continue  # An edit invalidated an existing selection; keep the old take in Cast.
                    raise
                checked_selected[segment_id] = take_id
            if len(segments_value) == 1:
                selected_take_id = checked_selected.get(segments_value[0]["id"])
            else:
                selected_take_id = None
            normalized.append({**source,
                "line_id": line_id, "line_type": kind, "character_id": character_id,
                "identity_id": identity_id, "engine_id": engine_id,
                "reference_version_id": identity["reference"]["id"] if identity["reference"] else None,
                "voice_identity": voice_identity,
                "text": text, "language": language, "direction": direction.strip(),
                "pause_before_ms": pause_before, "pause_after_ms": pause_after,
                "take_count": take_count, "selected_take_id": selected_take_id,
                "selected_takes": checked_selected, "target_duration_ms": target_duration,
                "settings": settings, "use_pronunciations": use_pronunciations,
                "segments": segments_value,
            })
        base = {**payload, "schema_version": SCHEMA_VERSION, "scene_id": scene_id, "name": name,
                "revision": revision, "lines": normalized}
        # Incoming updates carry the last saved checksum. It is metadata for the
        # old revision and must not become part of the newly calculated digest.
        base.pop("checksum", None)
        base["checksum"] = digest_json(base)
        return base

    def create_scene(self, payload):
        scene_id, stamp = new_id(), utc_now()
        scene = self._normalize_scene(payload, scene_id, 1)
        with self._connect() as db:
            db.execute("INSERT INTO scenes VALUES(?,?,?,?,?,?,?)", (scene_id, scene["name"], 1,
                       canonical_json(scene), scene["checksum"], stamp, stamp))
        self._write_scene_file(scene)
        return scene

    def update_scene(self, scene_id, payload, expected_revision=None):
        scene_id = self._validate_scene_id(scene_id)
        current = self.get_scene(scene_id)
        if expected_revision is not None and expected_revision != current["revision"]:
            raise ValueError(f"Scene revision conflict: current revision is {current['revision']}.")
        scene = self._normalize_scene(payload, scene_id, current["revision"] + 1, previous=current)
        stamp = utc_now()
        with self._connect() as db:
            changed = db.execute("UPDATE scenes SET name=?,revision=?,manifest_json=?,checksum=?,updated_at=? WHERE id=? AND revision=?",
                                 (scene["name"], scene["revision"], canonical_json(scene), scene["checksum"], stamp,
                                  scene_id, current["revision"])).rowcount
            if changed != 1:
                raise ValueError("Scene changed while it was being edited; reload the latest revision.")
        self._write_scene_file(scene)
        return scene

    @staticmethod
    def _validate_scene_id(scene_id):
        if not isinstance(scene_id, str) or not SCENE_ID_RE.fullmatch(scene_id):
            raise ValueError("Invalid scene ID.")
        return scene_id

    def _write_scene_file(self, scene):
        self._atomic_json(self._manifest_path(scene["scene_id"]), scene)

    def get_scene(self, scene_id):
        scene_id = self._validate_scene_id(scene_id)
        with self._connect() as db:
            row = db.execute("SELECT manifest_json,checksum FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if row is None:
            raise KeyError("Scene not found.")
        scene = json.loads(row["manifest_json"])
        actual = scene.pop("checksum", None)
        calculated = digest_json(scene)
        scene["checksum"] = actual
        if actual != row["checksum"] or actual != calculated:
            raise RuntimeError(f"Scene manifest checksum mismatch for {scene_id}.")
        return scene

    def list_scenes(self):
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM scenes ORDER BY updated_at DESC,id")]
        return [self.get_scene(scene_id) for scene_id in ids]

    def export_json(self, scene_id):
        scene = self.get_scene(scene_id)
        return (json.dumps(scene, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def export_csv(self, scene_id):
        scene = self.get_scene(scene_id)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for line in scene["lines"]:
            writer.writerow({
                "schema_version": SCHEMA_VERSION, "scene_name": scene["name"],
                **{key: line.get(key, "") for key in ("line_id", "line_type", "character_id", "identity_id",
                "engine_id", "reference_version_id", "text", "language", "direction", "pause_before_ms", "pause_after_ms",
                    "take_count", "selected_take_id", "target_duration_ms")},
                "selected_takes_json": canonical_json(line.get("selected_takes", {})),
                "settings_json": canonical_json(line.get("settings", {})),
                "use_pronunciations": str(bool(line.get("use_pronunciations", False))).lower(),
            })
        return output.getvalue().encode("utf-8-sig")

    def import_csv(self, raw):
        try:
            text = raw.decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text, newline="")))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"Invalid UTF-8 scene CSV: {exc}") from exc
        if not rows:
            raise ValueError("Scene CSV has no data rows.")
        if any(name not in (rows[0] or {}) for name in ("line_id", "identity_id", "text")):
            raise ValueError("Scene CSV needs line_id, identity_id, and text columns.")
        for index, row in enumerate(rows):
            version = (row.get("schema_version") or "").strip()
            if version and version != str(SCHEMA_VERSION):
                raise ValueError(f"Scene CSV row {index + 2} uses unsupported schema version {version!r}.")
        lines = []
        for index, row in enumerate(rows):
            try:
                settings = json.loads(row.get("settings_json") or "{}")
                selected_takes = json.loads(row.get("selected_takes_json") or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Scene CSV row {index + 2} has invalid JSON in a settings/selected_takes column.") from exc
            def optional_int(name):
                value = (row.get(name) or "").strip()
                return int(value) if value else None
            pronunciation_flag = (row.get("use_pronunciations") or "false").strip().lower()
            if pronunciation_flag not in {"true", "false", ""}:
                raise ValueError(f"Scene CSV row {index + 2} has invalid use_pronunciations value {pronunciation_flag!r}.")
            lines.append({
                "line_id": row.get("line_id"), "line_type": row.get("line_type") or "dialogue",
                "character_id": row.get("character_id") or None, "identity_id": row.get("identity_id"),
                "engine_id": row.get("engine_id") or None,
                "reference_version_id": row.get("reference_version_id") or None, "text": row.get("text"),
                "language": row.get("language") or "English", "direction": row.get("direction") or "",
                "pause_before_ms": int(row.get("pause_before_ms") or 0),
                "pause_after_ms": int(row.get("pause_after_ms") or 0), "take_count": int(row.get("take_count") or 1),
                "selected_take_id": row.get("selected_take_id") or None, "selected_takes": selected_takes,
                "target_duration_ms": optional_int("target_duration_ms"), "settings": settings,
                "use_pronunciations": pronunciation_flag == "true",
            })
        return {"schema_version": SCHEMA_VERSION, "name": rows[0].get("scene_name") or "Imported scene", "lines": lines}

    def script_manifest(self, content, fmt, speakers, narrator=None, name=None):
        """A scene manifest from a plain-text or Fountain script (script_import.parse_script), and a report of what it left out.

        speakers maps every speaker the script names to {"identity_id": ...}, or to None to leave that speaker's lines out; a
        speaker missing from the map is refused, so no line goes to a voice nobody chose. narrator is the voice for the lines
        with no speaker (plain-text lines with no name, Fountain action), or None to leave them out. Each line gets the
        defaults a new line gets on the page: the voice's default acting direction when the script gives none, 250 ms after
        it, one take, and the model's own settings. A direction for a model that takes none is left out and reported."""
        script = parse_script(content, fmt)
        if not script["lines"]:
            unread = len(script["unread"])
            raise ValueError("The script has no lines to import." + (f" {unread} line(s) could not be read." if unread else ""))
        if not isinstance(speakers, dict) or any(not isinstance(key, str) for key in speakers):
            raise ValueError("speakers must map each speaker's name to a voice, or to null to leave their lines out.")
        chosen = {" ".join(key.split()).casefold(): value for key, value in speakers.items()}
        named = {speaker["name"].casefold() for speaker in script["speakers"]}
        missing = [speaker["name"] for speaker in script["speakers"] if speaker["name"].casefold() not in chosen]
        if missing:
            raise ValueError("Choose a voice for each speaker, or leave their lines out: " + ", ".join(missing) + ".")
        strangers = sorted(key for key in chosen if key not in named)
        if strangers:
            raise ValueError("These names are not speakers in the script: " + ", ".join(strangers) + ".")
        identities = {}

        def voice(choice, label):
            if choice is None:
                return None
            if not isinstance(choice, dict) or not isinstance(choice.get("identity_id"), str):
                raise ValueError(f"The voice for {label} must be an object with an identity_id, or null to leave the lines out.")
            identity_id = choice["identity_id"]
            if identity_id not in identities:
                try:
                    identities[identity_id] = self.cast.get_generation_identity(identity_id, None)
                except (ValueError, KeyError) as exc:
                    raise ValueError(f"The voice for {label}: {exc}") from exc
            return identities[identity_id]

        width = max(3, len(str(len(script["lines"]))))
        lines, left_out, directions = [], [], []
        for line in script["lines"]:
            speaker = line["speaker"]
            identity = voice(chosen[speaker.casefold()] if speaker else narrator, speaker or "narration")
            where = {"number": line["number"], "speaker": speaker, "text": line["text"][:200]}
            if identity is None:
                left_out.append({**where, "reason": "its speaker is left out" if speaker else "no narrator voice was chosen"})
                continue
            if len(line["text"]) > MAX_LINE_TEXT:
                left_out.append({**where, "reason": f"longer than {MAX_LINE_TEXT:,} characters"})
                continue
            direction = line["direction"] or (identity.get("engine_options") or {}).get("default_direction") or ""
            if direction and identity["engine_id"] not in DIRECTION_ENGINES:
                if line["direction"]:
                    directions.append({**where, "direction": line["direction"]})
                direction = ""
            lines.append({"line_id": f"line-{len(lines) + 1:0{width}d}", "line_type": line["kind"],
                          "character_id": identity["character_id"], "identity_id": identity["id"], "text": line["text"],
                          "direction": direction, "pause_before_ms": 0, "pause_after_ms": 250, "take_count": 1,
                          "settings": {}, "use_pronunciations": False})
        if not lines:
            raise ValueError("Every line of the script is left out; choose a voice for at least one speaker.")
        if len(lines) > MAX_SCENE_LINES:
            raise ValueError(f"The script has {len(lines)} lines to import, and a scene holds at most {MAX_SCENE_LINES}. "
                             "Split the script, or leave some speakers out.")
        title = " ".join(str(name or script["title"] or "Imported script").split())[:120]
        report = {"lines": len(lines), "left_out": left_out, "unread": script["unread"], "directions_left_out": directions}
        return {"schema_version": SCHEMA_VERSION, "name": title, "lines": lines}, report

    def segment_preview(self, scene_id, line_id=None):
        scene = self.get_scene(scene_id)
        return [{"line_id": line["line_id"], "segments": line["segments"]}
                for line in scene["lines"] if line_id is None or line["line_id"] == line_id]

    def create_job(self, scene_id, line_ids=None, segment_ids=None):
        scene = self.get_scene(scene_id)
        selected_segments = None
        if line_ids is not None:
            if not isinstance(line_ids, list) or not line_ids or any(not isinstance(item, str) for item in line_ids):
                raise ValueError("line_ids must be a non-empty list of stable scene line IDs.")
            requested = set(line_ids)
            unknown = requested - {line["line_id"] for line in scene["lines"]}
            if unknown:
                raise ValueError(f"Unknown scene line IDs: {', '.join(sorted(unknown))}.")
            lines = [line for line in scene["lines"] if line["line_id"] in requested]
        else:
            lines = scene["lines"]
        if segment_ids is not None:
            if line_ids is not None:
                raise ValueError("Choose line_ids or segment_ids for regeneration, not both.")
            if not isinstance(segment_ids, list) or not segment_ids or any(not isinstance(item, str) for item in segment_ids):
                raise ValueError("segment_ids must be a non-empty list in line_id/segment_id form.")
            selected_segments = set()
            for value in segment_ids:
                if value.count("/") != 1:
                    raise ValueError(f"Invalid segment selector {value!r}; use line_id/segment_id.")
                selected_segments.add(tuple(value.split("/", 1)))
            valid_segments = {(line["line_id"], segment["id"]) for line in scene["lines"] for segment in line["segments"]}
            unknown_segments = selected_segments - valid_segments
            if unknown_segments:
                raise ValueError("Unknown scene segment selectors: " + ", ".join(f"{line}/{segment}" for line, segment in sorted(unknown_segments)) + ".")
        job_id, stamp = new_id(), utc_now()
        for line in lines:
            try:
                self.cast.assert_identity_usable(line["identity_id"], line["reference_version_id"])
            except ValueError as exc:
                raise ValueError(f"Line {line['line_id']}: {exc}") from exc
        operations = []
        scene_order_by_id = {line["line_id"]: index for index, line in enumerate(scene["lines"])}
        for line in lines:
            scene_order = scene_order_by_id[line["line_id"]]
            for segment_index, segment in enumerate(line["segments"]):
                if selected_segments is not None and (line["line_id"], segment["id"]) not in selected_segments:
                    continue
                cast_line_id = f"{scene_id[:12]}:{line['line_id']}"
                if len(line["segments"]) > 1:
                    cast_line_id += f":{segment_index + 1:03d}"
                segment_id = segment["id"]
                for take_index in range(1, line["take_count"] + 1):
                    identity = self.cast.get_identity(line["identity_id"])
                    operations.append((identity["engine_id"], line["reference_version_id"] or "", scene_order,
                                       segment_index, take_index, new_id(), line, segment, cast_line_id,
                                       job_id + ":" + line["line_id"] + ":" + segment_id + ":" + str(take_index)))
        operations.sort(key=lambda item: item[:5])
        rows = [(row_id, ordinal, scene_order, line, segment, segment_index, take_index, cast_line_id, request_key, stamp)
                for ordinal, (_engine, _reference, scene_order, segment_index, take_index, row_id, line, segment,
                              cast_line_id, request_key) in enumerate(operations)]
        with self._connect() as db:
            db.execute("INSERT INTO jobs(id,scene_id,scene_revision,scene_checksum,scene_snapshot_json,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                       (job_id, scene_id, scene["revision"], scene["checksum"], canonical_json(scene), stamp, stamp))
            db.executemany("""INSERT INTO job_rows
                (id,job_id,ordinal,scene_order,line_id,segment_id,segment_index,take_index,cast_line_id,identity_id,
                 reference_version_id,text,language,direction,settings_json,use_pronunciations,status,
                 request_key,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)""",
                [(row_id, job_id, ordinal, scene_order, line["line_id"], segment["id"], segment_index, take_index,
                  cast_line_id, line["identity_id"], line["reference_version_id"], segment["text"],
                  line["language"], line["direction"], canonical_json(line["settings"]),
                  int(line["use_pronunciations"]), request_key, stamp, stamp)
                 for row_id, ordinal, scene_order, line, segment, segment_index, take_index, cast_line_id, request_key, stamp in rows])
        self._write_job_manifest(job_id)
        return self.get_job(job_id)

    def get_job(self, job_id):
        _validate_id(job_id, "batch job id")
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("Scene batch job not found.")
            result = dict(row)
            result["scene_snapshot"] = json.loads(result.pop("scene_snapshot_json"))
            rows = [dict(item) for item in db.execute("SELECT * FROM job_rows WHERE job_id=? ORDER BY ordinal", (job_id,))]
        for item in rows:
            item["settings"] = json.loads(item.pop("settings_json"))
            item["use_pronunciations"] = bool(item["use_pronunciations"])
        summaries = self.cast.take_summaries_for([item["take_id"] for item in rows if item["take_id"]])
        for item in rows:
            summary = summaries.get(item["take_id"]) or {}
            item["waveform_checks"] = summary.get("waveform_checks")
            item["loudness"] = summary.get("loudness")
            item["retirement"] = summary.get("retirement")
            item["purged"] = bool(summary.get("purged"))
        engine_of = {}
        for item in rows:
            identity_id = item["identity_id"]
            if identity_id not in engine_of:
                try:
                    engine_of[identity_id] = self.cast.get_identity(identity_id)["engine_id"]
                except KeyError:
                    engine_of[identity_id] = None
            item["engine_id"] = engine_of[identity_id]
        result["rows"] = rows
        result["models"] = batch_models(rows, self.entries)
        result["total"] = len(rows)
        result["completed"] = sum(item["status"] == "completed" for item in rows)
        result["failed"] = sum(item["status"] == "failed" for item in rows)
        result["cancelled"] = sum(item["status"] == "cancelled" for item in rows)
        result["queued"] = sum(item["status"] == "queued" for item in rows)
        result["running"] = sum(item["status"] == "running" for item in rows)
        result["progress"] = ((result["completed"] + result["failed"] + result["cancelled"]) / result["total"]
                               if result["total"] else 1.0)
        result["manifest_checksum"] = digest_json(self._job_manifest_payload(result))
        result["manifest_path"] = str(self._job_manifest_path(job_id))
        return result

    def list_jobs(self, scene_id):
        scene_id = self._validate_scene_id(scene_id)
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE scene_id=? ORDER BY created_at DESC,rowid DESC", (scene_id,))]
        return [self.get_job(job_id) for job_id in ids]

    def _job_manifest_path(self, job_id):
        with self._connect() as db:
            row = db.execute("SELECT scene_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        scene_id = row[0] if row else "unknown"
        return self.output_root / scene_id / "jobs" / f"{job_id}.json"

    @staticmethod
    def _job_manifest_payload(job):
        return {
            "schema_version": 1, "job_id": job["id"], "scene_id": job["scene_id"],
            "scene_revision": job["scene_revision"], "scene_checksum": job["scene_checksum"],
            "scene_snapshot": job["scene_snapshot"], "status": job["status"],
            "progress": {key: job[key] for key in ("total", "completed", "failed", "cancelled", "queued", "running")},
            "rows": [{key: row.get(key) for key in ("id", "ordinal", "scene_order", "line_id", "segment_id", "segment_index",
                "take_index", "cast_line_id", "identity_id", "reference_version_id", "text", "language", "direction",
                "settings", "use_pronunciations", "status", "request_key", "take_id", "attempts", "error")}
                     for row in job["rows"]],
        }

    def _write_job_manifest(self, job_id):
        path = self._job_manifest_path(job_id)
        with _path_lock(path):
            job = self.get_job(job_id)
            payload = self._job_manifest_payload(job)
            payload["checksum"] = digest_json(payload)
            self._atomic_json(path, payload)
            return payload

    def verify_job_manifest(self, job_id):
        path = self._job_manifest_path(job_id)
        with _path_lock(path):
            job = self.get_job(job_id)
            expected = self._job_manifest_payload(job)
            expected_checksum = digest_json(expected)
            if not path.is_file():
                self._write_job_manifest(job_id)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            saved = payload.pop("checksum", None)
            actual = digest_json(payload)
            if saved != actual or actual != expected_checksum:
                self._write_job_manifest(job_id)
                payload = json.loads(path.read_text(encoding="utf-8"))
                saved = payload.pop("checksum", None)
                actual = digest_json(payload)
            return saved == actual == expected_checksum, {"saved": saved, "calculated": actual, "path": str(path)}

    def _update_job_row(self, row_id, status, *, take_id=None, error="", increment_attempt=False):
        stamp = utc_now()
        with self._connect() as db:
            update_attempt = ",attempts=attempts+1" if increment_attempt else ""
            db.execute(f"UPDATE job_rows SET status=?,take_id=COALESCE(?,take_id),error=?,updated_at=?{update_attempt} WHERE id=?",
                       (status, take_id, error, stamp, row_id))
            row = db.execute("SELECT job_id FROM job_rows WHERE id=?", (row_id,)).fetchone()
            if row:
                db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, row[0]))
                job_id = row[0]
            else:
                raise KeyError("Batch job row not found.")
        self._write_job_manifest(job_id)

    def mark_row_running(self, row_id):
        self._update_job_row(row_id, "running", error="", increment_attempt=True)

    def mark_row_completed(self, row_id, take_id):
        _validate_id(take_id, "take id")
        self._update_job_row(row_id, "completed", take_id=take_id, error="")

    def mark_row_failed(self, row_id, error):
        self._update_job_row(row_id, "failed", error=str(error)[:2000])

    def claim_next_row(self, job_id):
        stamp = utc_now()
        claimed = None
        changed = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError("Scene batch job not found.")
            if job["status"] != "running":
                return None
            if job["cancel_requested"]:
                db.execute("UPDATE job_rows SET status='cancelled',updated_at=? WHERE job_id=? AND status='queued'", (stamp, job_id))
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (stamp, job_id))
                changed = True
            elif job["pause_requested"]:
                db.execute("UPDATE jobs SET status='paused',pause_requested=0,updated_at=? WHERE id=?", (stamp, job_id))
                changed = True
            else:
                row = db.execute("SELECT id FROM job_rows WHERE job_id=? AND status='queued' ORDER BY ordinal LIMIT 1", (job_id,)).fetchone()
                if row is None:
                    remaining = db.execute("SELECT COUNT(*) FROM job_rows WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
                    db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", ("failed" if remaining else "completed", stamp, job_id))
                    changed = True
                else:
                    db.execute("UPDATE job_rows SET status='running',attempts=attempts+1,error='',updated_at=? WHERE id=?", (stamp, row[0]))
                    db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, job_id))
                    claimed = row[0]
                    changed = True
        if changed:
            self._write_job_manifest(job_id)
        return claimed

    def get_job_row(self, row_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM job_rows WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise KeyError("Batch job row not found.")
        item = dict(row)
        item["settings"] = json.loads(item.pop("settings_json"))
        item["use_pronunciations"] = bool(item["use_pronunciations"])
        return item

    def queued_job_ids(self):
        with self._connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id")]

    def start_queued_job(self, job_id):
        stamp = utc_now()
        with self._connect() as db:
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'", (stamp, job_id))
        self._write_job_manifest(job_id)

    def request_pause(self, job_id):
        job = self.get_job(job_id)
        stamp = utc_now()
        with self._connect() as db:
            if job["status"] == "queued":
                db.execute("UPDATE jobs SET status='paused',pause_requested=0,updated_at=? WHERE id=?", (stamp, job_id))
            elif job["status"] == "running":
                db.execute("UPDATE jobs SET pause_requested=1,updated_at=? WHERE id=?", (stamp, job_id))
            else:
                raise ValueError(f"Cannot pause a {job['status']} batch job.")
        self._write_job_manifest(job_id)
        return self.get_job(job_id)

    def resume(self, job_id):
        job = self.get_job(job_id)
        if job["status"] != "paused":
            raise ValueError(f"Cannot resume a {job['status']} batch job.")
        stamp = utc_now()
        with self._connect() as db:
            db.execute("UPDATE jobs SET status='queued',pause_requested=0,cancel_requested=0,updated_at=? WHERE id=?", (stamp, job_id))
        self._write_job_manifest(job_id)
        return self.get_job(job_id)

    def cancel(self, job_id):
        job = self.get_job(job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Cannot cancel a {job['status']} batch job.")
        stamp = utc_now()
        with self._connect() as db:
            if job["status"] == "queued" or job["status"] == "paused":
                db.execute("UPDATE job_rows SET status='cancelled',updated_at=? WHERE job_id=? AND status='queued'", (stamp, job_id))
                db.execute("UPDATE jobs SET status='cancelled',cancel_requested=0,updated_at=? WHERE id=?", (stamp, job_id))
            else:
                db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (stamp, job_id))
        self._write_job_manifest(job_id)
        return self.get_job(job_id)

    def retry_failed(self, job_id):
        job = self.get_job(job_id)
        if job["status"] != "failed" or not job["failed"]:
            raise ValueError("This batch has no failed rows to retry.")
        stamp = utc_now()
        with self._connect() as db:
            db.execute("UPDATE job_rows SET status='queued',error='',updated_at=? WHERE job_id=? AND status='failed'", (stamp, job_id))
            db.execute("UPDATE jobs SET status='queued',error='',cancel_requested=0,pause_requested=0,updated_at=? WHERE id=?", (stamp, job_id))
        self._write_job_manifest(job_id)
        return self.get_job(job_id)

    def select_take(self, scene_id, line_id, take_id, segment_id=None):
        scene = self.get_scene(scene_id)
        take_id = _validate_id(take_id, "take id")
        line = next((line for line in scene["lines"] if line["line_id"] == line_id), None)
        if line is None:
            raise KeyError("Scene line not found.")
        take = self.cast.get_take(take_id)
        if take["retired"]:
            raise ValueError("This take is retired; choose another take for this line.")
        if take["identity_id"] != line["identity_id"]:
            raise ValueError("Selected take must use this scene line's voice identity.")
        segments = line["segments"]
        if segment_id is None:
            if len(segments) != 1:
                raise ValueError("Choose a segment ID when selecting a take for a segmented line.")
            segment_id = segments[0]["id"]
        if segment_id not in {segment["id"] for segment in segments}:
            raise ValueError("Selected segment does not belong to this scene line.")
        segment = next(item for item in segments if item["id"] == segment_id)
        spoken = self.cast.apply_pronunciations(segment["text"], line["character_id"]) if line["use_pronunciations"] else segment["text"]
        validate_selected_take(scene_id, line, segment, take, spoken)
        line["selected_takes"] = {**line.get("selected_takes", {}), segment_id: take_id}
        line["selected_take_id"] = take_id if len(segments) == 1 else None
        self.cast.set_take_state(take_id, status="selected")
        return self.update_scene(scene_id, scene, expected_revision=scene["revision"])

    def line_states(self, scene_id):
        """Where every line of a scene stands, in one answer for the scenes table.

        Each line, and each of its segments, has a status: not_rendered; waiting (a batch will reach it); rendering;
        failed (the latest batch to try its current text failed, and `reason` says why); choose_take (it has a take that
        fits its current text, voice and settings, and none is chosen); out_of_date (its chosen take, or every take it
        has, was made before its text, voice or settings changed, and `reason` says what changed); or done (its chosen
        take still fits). A line has the first of rendering, waiting, failed, out_of_date, not_rendered, choose_take and
        done that one of its segments has. Each segment lists its takes newest first, leaving out retired ones that are
        not chosen."""
        scene = self.get_scene(scene_id)
        prefix = scene_id[:12] + ":"
        takes_by_line = {}
        for take in self.cast.list_takes_with_line_prefix(prefix):
            takes_by_line.setdefault(take["line_id"], []).append(take)
        known = {take["id"] for takes in takes_by_line.values() for take in takes}
        others = {take["id"]: take for take in self.cast.get_takes(
            [take_id for line in scene["lines"] for take_id in (line.get("selected_takes") or {}).values()
             if take_id not in known])}
        attempts = self._segment_attempts(scene_id)
        pronunciation_lists = {}
        lines = []
        for line in scene["lines"]:
            segments = []
            if line["use_pronunciations"] and line["character_id"] not in pronunciation_lists:
                pronunciation_lists[line["character_id"]] = self.cast.list_pronunciations(line["character_id"])
            for index, segment in enumerate(line["segments"]):
                cast_line_id = prefix + line["line_id"] + (f":{index + 1:03d}" if len(line["segments"]) > 1 else "")
                spoken = (self.cast.apply_pronunciations(segment["text"], line["character_id"],
                                                         pronunciation_lists[line["character_id"]])
                          if line["use_pronunciations"] else segment["text"])
                segments.append(self._segment_state(scene_id, line, segment, spoken, takes_by_line.get(cast_line_id, []),
                                                    others, attempts.get((line["line_id"], segment["id"]))))
            status = next(item for item in LINE_STATUS_ORDER if any(part["status"] == item for part in segments))
            reason = next((part["reason"] for part in segments if part["status"] == status and part["reason"]), "")
            lines.append({"line_id": line["line_id"], "status": status, "reason": reason,
                          "segments_done": sum(part["status"] == "done" for part in segments), "segments": segments})
        return {"scene_id": scene_id, "revision": scene["revision"], "lines": lines}

    def _segment_attempts(self, scene_id):
        """For each (line id, segment id) a batch of this scene has held: `active` (rendering, waiting or None) and `rows`,
        the rows of the newest batch that tried it, leaving out cancelled ones."""
        with self._connect() as db:
            rows = db.execute("""SELECT r.line_id,r.segment_id,r.text,r.identity_id,r.status,r.error,r.job_id,
                                        j.status AS job_status
                                 FROM job_rows r JOIN jobs j ON j.id=r.job_id WHERE j.scene_id=?
                                 ORDER BY j.created_at DESC,j.rowid DESC,r.ordinal""", (scene_id,)).fetchall()
        found = {}
        for row in rows:
            entry = found.setdefault((row["line_id"], row["segment_id"]), {"active": None, "job_id": None, "rows": []})
            if row["job_status"] in ACTIVE_JOB_STATUSES and row["status"] == "running":
                entry["active"] = "rendering"
            elif row["job_status"] in ACTIVE_JOB_STATUSES and row["status"] == "queued" and entry["active"] is None:
                entry["active"] = "waiting"
            if row["status"] == "cancelled":
                continue
            if entry["job_id"] is None:
                entry["job_id"] = row["job_id"]
            if entry["job_id"] == row["job_id"]:
                entry["rows"].append(row)
        return found

    @staticmethod
    def _segment_state(scene_id, line, segment, spoken, takes, others, attempt):
        chosen_id = (line.get("selected_takes") or {}).get(segment["id"])
        listed, newest_problem = [], None
        for take in reversed(takes):
            if take["retired"] and take["id"] != chosen_id:
                continue
            problem = take_problem(scene_id, line, segment, take, spoken)
            fits = problem is None and not take["retired"]
            if problem is not None and newest_problem is None:
                newest_problem = problem
            listed.append({"id": take["id"], "created_at": take["created_at"],
                           "duration_seconds": take["duration_seconds"], "audio_url": take["audio_url"],
                           "waveform_checks": take["waveform_checks"], "chosen": take["id"] == chosen_id, "fits": fits,
                           "reason": ("This take is retired." if take["retired"] else
                                      STALE_REASONS.get(problem[0], problem[1]) if problem else "")})
        chosen = next((take for take in takes if take["id"] == chosen_id), None) or others.get(chosen_id)
        chosen_problem = None
        if chosen_id and chosen is None:
            chosen_problem = "The chosen take is no longer in Cast."
        elif chosen is not None and chosen["retired"]:
            chosen_problem = "The chosen take was retired."
        elif chosen is not None:
            problem = take_problem(scene_id, line, segment, chosen, spoken)
            chosen_problem = STALE_REASONS.get(problem[0], problem[1]) if problem else None
        tried = [row for row in (attempt or {}).get("rows", [])
                 if row["text"] == segment["text"] and row["identity_id"] == line["identity_id"]]
        failed = next((row for row in tried if row["status"] == "failed"), None)
        if any(row["status"] == "completed" for row in tried):
            failed = None
        state = {"segment_id": segment["id"], "status": "not_rendered", "reason": "",
                 "chosen_take_id": chosen["id"] if chosen is not None else None, "takes": listed}
        if attempt and attempt["active"]:
            state["status"] = attempt["active"]
        elif failed is not None:
            state.update(status="failed", reason=failed["error"] or "The model could not make this take.")
        elif chosen is not None and chosen_problem is None:
            state["status"] = "done"
        elif any(item["fits"] for item in listed):
            state.update(status="choose_take", reason=chosen_problem or "")
        elif chosen_problem:
            state.update(status="out_of_date", reason=chosen_problem)
        elif newest_problem is not None:
            state.update(status="out_of_date", reason=STALE_REASONS.get(newest_problem[0], newest_problem[1]))
        return state

    def choose_newest_takes(self, scene_id, line_ids=None):
        """For every segment with no chosen take that fits, choose its newest take that does, all in one new revision.
        Only the given lines when line_ids is a list. Returns (scene, how many segments got a take, how many lines)."""
        scene = self.get_scene(scene_id)
        if line_ids is not None:
            if not isinstance(line_ids, list) or any(not isinstance(item, str) for item in line_ids):
                raise ValueError("line_ids must be a list of scene line IDs.")
            unknown = set(line_ids) - {line["line_id"] for line in scene["lines"]}
            if unknown:
                raise ValueError(f"Unknown scene line IDs: {', '.join(sorted(unknown))}.")
        states = {item["line_id"]: item for item in self.line_states(scene_id)["lines"]}
        picked, lines = [], set()
        for line in scene["lines"]:
            if line_ids is not None and line["line_id"] not in line_ids:
                continue
            for segment in states[line["line_id"]]["segments"]:
                if any(take["chosen"] and take["fits"] for take in segment["takes"]):
                    continue
                newest = next((take for take in segment["takes"] if take["fits"]), None)
                if newest is None:
                    continue
                line["selected_takes"] = {**(line.get("selected_takes") or {}), segment["segment_id"]: newest["id"]}
                if len(line["segments"]) == 1:
                    line["selected_take_id"] = newest["id"]
                picked.append(newest["id"])
                lines.add(line["line_id"])
        if not picked:
            return scene, 0, 0
        scene = self.update_scene(scene_id, scene, expected_revision=scene["revision"])
        self.cast.mark_takes_selected(picked)
        return scene, len(picked), len(lines)

    def assemble_preview(self, scene_id):
        scene = self.get_scene(scene_id)
        recent_jobs = self.list_jobs(scene_id)
        if recent_jobs and recent_jobs[0]["status"] == "failed" and recent_jobs[0]["failed"]:
            raise ValueError(f"Scene export is blocked because the latest batch has {recent_jobs[0]['failed']} failed row(s); retry them first.")
        audio_parts = []
        output_rate = None
        missing = []
        manifest_lines = []
        for line in scene["lines"]:
            line_takes = []
            for segment in line["segments"]:
                take_id = line.get("selected_takes", {}).get(segment["id"])
                if not take_id:
                    missing.append(f"{line['line_id']}/{segment['id']}")
                    continue
                try:
                    take = self.cast.get_take(take_id)
                except KeyError as exc:
                    raise ValueError(f"Selected take {take_id} no longer exists; choose another take.") from exc
                if take["retired"]:
                    raise ValueError(f"Selected take {take_id} is retired; choose another take before assembling.")
                spoken = self.cast.apply_pronunciations(segment["text"], line["character_id"]) if line["use_pronunciations"] else segment["text"]
                validate_selected_take(scene_id, line, segment, take, spoken)
                media_path = self.cast.get_media_path("take", take_id)
                raw_audio = media_path.read_bytes()
                if hashlib.sha256(raw_audio).hexdigest() != take["audio_sha256"]:
                    raise ValueError(f"Selected take {take_id} failed its saved audio checksum.")
                audio = sf.read(io.BytesIO(raw_audio), dtype="float32")[0]
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                rate = take["sample_rate"]
                if output_rate is None:
                    output_rate = rate
                if rate != output_rate:
                    divisor = np.gcd(rate, output_rate)
                    audio = resample_poly(audio, output_rate // divisor, rate // divisor).astype(np.float32)
                line_takes.append((segment, take, audio))
            if len(line_takes) != len(line["segments"]):
                continue
            if line["pause_before_ms"]:
                audio_parts.append(np.zeros(round(output_rate * line["pause_before_ms"] / 1000), dtype=np.float32))
            for segment, take, audio in line_takes:
                audio_parts.append(audio)
                manifest_lines.append({"line_id": line["line_id"], "segment_id": segment["id"],
                                       "take_id": take["id"],
                                       "audio_sha256": take["audio_sha256"], "duration_seconds": take["duration_seconds"]})
            if line["pause_after_ms"]:
                audio_parts.append(np.zeros(round(output_rate * line["pause_after_ms"] / 1000), dtype=np.float32))
        if missing:
            raise ValueError("Scene preview requires a selected successful take for every line segment: " + ", ".join(missing))
        if not audio_parts or output_rate is None:
            raise ValueError("Scene preview has no selected audio.")
        audio = np.concatenate(audio_parts)
        scene_out = self.output_root / scene_id
        scene_out.mkdir(parents=True, exist_ok=True)
        wav_path = scene_out / f"scene-r{scene['revision']}-{uuid.uuid4().hex[:8]}.wav"
        synthetic_label.write_wav(wav_path, audio, output_rate)
        file_hash = hashlib.sha256(wav_path.read_bytes()).hexdigest()
        mastered_path = scene_out / f"scene-mastered-r{scene['revision']}-{uuid.uuid4().hex[:8]}.wav"
        peak_ceiling = 10 ** (-1.0 / 20.0)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        peak_gain = peak_ceiling / peak if peak > 0 else 1.0
        input_lufs = None
        loudness_gain = 1.0
        if len(audio) / output_rate >= 0.4 and peak > 0:
            measured = float(pyln.Meter(output_rate).integrated_loudness(audio))
            if np.isfinite(measured):
                input_lufs = measured
                loudness_gain = 10 ** ((-18.0 - input_lufs) / 20.0)
        applied_gain = min(loudness_gain, 10 ** (12.0 / 20.0), peak_gain)
        mastered_audio = np.clip(audio * applied_gain, -peak_ceiling, peak_ceiling).astype(np.float32)
        synthetic_label.write_wav(mastered_path, mastered_audio, output_rate)
        mastered_hash = hashlib.sha256(mastered_path.read_bytes()).hexdigest()
        output_lufs = None
        if input_lufs is not None and len(mastered_audio) / output_rate >= 0.4:
            measured_output = float(pyln.Meter(output_rate).integrated_loudness(mastered_audio))
            if np.isfinite(measured_output):
                output_lufs = measured_output
        mastered_peak = float(np.max(np.abs(mastered_audio))) if len(mastered_audio) else 0.0
        mastering = {
            "method": "scene-level EBU R128 loudness normalization with sample-peak ceiling",
            "target_lufs": -18.0,
            "peak_ceiling_dbfs": -1.0,
            "max_gain_db": 12.0,
            "input_integrated_lufs": input_lufs,
            "output_integrated_lufs": output_lufs,
            "applied_gain_db": 20.0 * np.log10(applied_gain) if applied_gain > 0 else None,
            "sample_peak_dbfs": 20.0 * np.log10(mastered_peak) if mastered_peak > 0 else None,
            "compression_or_crossfade": False,
        }
        manifest = {"schema_version": 1, "scene_id": scene_id, "scene_revision": scene["revision"],
                    "scene_checksum": scene["checksum"], "assembly": "separate sequential speech calls with explicit silence; no crossfades",
                    "sample_rate": output_rate, "duration_seconds": len(audio) / output_rate,
                    "audio_path": str(wav_path), "audio_sha256": file_hash,
                    "mastered_audio_path": str(mastered_path), "mastered_audio_sha256": mastered_hash,
                    "mastering": mastering, "lines": manifest_lines}
        manifest["checksum"] = digest_json(manifest)
        manifest_path = wav_path.with_suffix(".json")
        self._atomic_json(manifest_path, manifest)
        return manifest


class SceneBatchRunner:
    """One sequential local HTTP client driving the same guarded performance API as the UI.

    Each row asks to wait for the model worker as a low-priority request (job_queue.py): a person's Perform or Generate goes
    ahead of the next row, and the batch then carries on. A row's request id is "scene-row-" and the row's id, so cancelling
    the batch can also stop the row it is rendering (current_request)."""

    ROW_WAIT_SECONDS = 3600

    def __init__(self, store: SceneStore, service_url):
        self.store = store
        self.service_url = service_url.rstrip("/")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._ownership = LocalOwnership(store.ownership_path)
        self._current = None  # (job id, request id) of the row being rendered

    def current_request(self, job_id):
        """The request id of the row this job is rendering now, or None."""
        current = self._current
        return current[1] if current and current[0] == job_id else None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voice-lab-scene-batch", daemon=True)
        self._thread.start()
        self._wake.set()

    def close(self, timeout=900):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def wake(self):
        self._wake.set()

    def _run(self):
        try:
            while not self._stop.is_set():
                if self._ownership.acquire():
                    self.store.recover_interrupted_jobs(self._ownership)
                    self._run_owned()
                    return
                self._wake.wait(0.5)
                self._wake.clear()
        finally:
            self._ownership.release()

    def _run_owned(self):
        while not self._stop.is_set():
            job_ids = self.store.queued_job_ids()
            if not job_ids:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            job_id = job_ids[0]
            self.store.start_queued_job(job_id)
            job_snapshot = self.store.get_job(job_id)["scene_snapshot"]
            snapshot_lines = {line["line_id"]: line for line in job_snapshot.get("lines", [])}
            while not self._stop.is_set():
                row_id = self.store.claim_next_row(job_id)
                if row_id is None:
                    break
                row = self.store.get_job_row(row_id)
                try:
                    request = {
                        "identity_id": row["identity_id"], "reference_version_id": row["reference_version_id"],
                        "line_id": row["cast_line_id"], "text": row["text"], "language": row["language"],
                        "direction": row["direction"], "settings": row["settings"],
                        "use_pronunciations": row["use_pronunciations"], "idempotency_key": row["request_key"],
                        "wait_seconds": self.ROW_WAIT_SECONDS, "wait_priority": "low",
                    }
                    scene_line = snapshot_lines.get(row["line_id"])
                    if scene_line and scene_line.get("voice_identity"):
                        request["identity_snapshot"] = scene_line["voice_identity"]
                    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
                    request_id = f"scene-row-{row_id}"
                    api_request = urllib.request.Request(self.service_url + "/api/performance", data=body,
                        headers={"Content-Type": "application/json", "X-Request-Id": request_id})
                    self._current = (job_id, request_id)
                    with urllib.request.urlopen(api_request, timeout=self.ROW_WAIT_SECONDS + 1800) as response:
                        wav = response.read()
                        take_id = response.headers.get("X-Take-Id")
                        if not wav.startswith(b"RIFF") or not take_id:
                            raise RuntimeError("Performance API returned no WAV take ID.")
                    self.store.mark_row_completed(row_id, take_id)
                except urllib.error.HTTPError as exc:
                    details = exc.read().decode("utf-8", "replace")[:1500]
                    if exc.code == 409:
                        # Another service owns the model, or the line was full or too slow: try the row again shortly.
                        self.store._update_job_row(row_id, "queued", error="Waiting for model ownership: " + details)
                        self._stop.wait(0.5)
                        continue
                    if exc.code == 499 and self.store.get_job(job_id)["cancel_requested"]:
                        self.store._update_job_row(row_id, "cancelled", error="Stopped when the batch was cancelled.")
                        continue
                    if exc.code == 499:
                        self.store.mark_row_failed(row_id, "Stopped before it finished. Try the failed lines again to render it.")
                        continue
                    self.store.mark_row_failed(row_id, f"HTTP {exc.code}: {details}")
                except Exception as exc:
                    self.store.mark_row_failed(row_id, f"{type(exc).__name__}: {exc}")
                finally:
                    self._current = None
            if self._stop.is_set():
                return
            job = self.store.get_job(job_id)
            if job["status"] == "running":
                # All work was either completed or failed; the claim operation finalizes it.
                self.store.claim_next_row(job_id)


def json_template():
    return {"schema_version": SCHEMA_VERSION, "name": "Untitled scene", "lines": [{
        "line_id": "line-001", "line_type": "dialogue", "character_id": "<character-id>",
        "identity_id": "<identity-id>", "engine_id": "qwen3-tts-1.7b-base",
        "reference_version_id": "<reference-version-id-or-null>",
        "text": "The exact words to speak.", "language": "English", "direction": "",
        "pause_before_ms": 0, "pause_after_ms": 250, "take_count": 1,
        "selected_take_id": None, "selected_takes": {}, "target_duration_ms": None,
        "settings": {"seed": 42}, "use_pronunciations": False,
    }]}


def csv_template():
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerow({"schema_version": SCHEMA_VERSION, "scene_name": "Untitled scene", "line_id": "line-001",
        "line_type": "narration", "character_id": "<character-id>", "identity_id": "<identity-id>",
        "engine_id": "qwen3-tts-1.7b-base", "reference_version_id": "<reference-version-id-or-empty>", "text": "The exact words to speak.",
        "language": "English", "direction": "", "pause_before_ms": 0, "pause_after_ms": 250,
        "take_count": 1, "selected_take_id": "", "selected_takes_json": "{}", "target_duration_ms": "",
        "settings_json": "{\"seed\":42}", "use_pronunciations": "false"})
    return output.getvalue().encode("utf-8-sig")
