"""Durable, loss-aware evaluation and blind-listening records."""

from __future__ import annotations

import hashlib
import itertools
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from db_snapshots import snapshot_before_migration
from evaluation_diagnostics import analyze_audio, compare_transcript
from runtime_folders import COMPARISONS_DIR, LEGACY_COMPARISONS_DIR, FolderMoveError, move_folder


SCHEMA_VERSION = 3
RATING_FIELDS = ("intelligibility", "identity_consistency", "acting_fit", "naturalness", "artifacts")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_json(value):
    return digest_bytes(canonical_json(value).encode("utf-8"))


def _rating_scores(scores):
    if not isinstance(scores, dict) or set(scores) != set(RATING_FIELDS):
        raise ValueError("Ratings must include exactly: " + ", ".join(RATING_FIELDS) + ".")
    for field, value in scores.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ValueError(f"{field} must be an integer from 1 through 5.")
    return {field: scores[field] for field in RATING_FIELDS}


def _contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def _relative(path, root):
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


class EvaluationStore:
    def __init__(self, db_path=None, *, lab_root=None, out_root=None):
        self.lab_root = Path(lab_root or Path(__file__).resolve().parent).resolve()
        self.out_root = Path(out_root or self.lab_root / "out").resolve()
        self.db_path = Path(db_path or self.lab_root / "data" / "evaluations" / "evaluations.sqlite3").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.migration_notes = []  # what moving the comparison folder did or could not do, as sentences for the service log
        self._init_db()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
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
                raise RuntimeError(f"Evaluation database schema {version} is newer than this voice-lab build.")
            snapshot_before_migration(db, self.db_path, version, SCHEMA_VERSION, "evaluations")
            # A database made by an earlier build can still hold two tables this build never creates or reads, legacy_sources
            # and legacy_records, left by a historical-report importer that was removed. They are left as they are.
            if version == 0:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        suite_id TEXT NOT NULL,
                        suite_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        manifest_path TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL UNIQUE,
                        summary_json TEXT NOT NULL,
                        manifest_json TEXT NOT NULL
                    );
                    CREATE TABLE items (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(id),
                        case_id TEXT NOT NULL,
                        repetition INTEGER NOT NULL,
                        engine_id TEXT NOT NULL,
                        expected_text TEXT NOT NULL,
                        raw_path TEXT NOT NULL,
                        raw_sha256 TEXT NOT NULL,
                        listening_path TEXT NOT NULL,
                        listening_sha256 TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        diagnostics_json TEXT NOT NULL,
                        transcript_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id, case_id, repetition, engine_id)
                    );
                    CREATE INDEX items_run_case ON items(run_id, case_id, repetition, engine_id);
                    CREATE TABLE blind_sessions (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(id),
                        evaluator TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('blind','revealed')),
                        created_at TEXT NOT NULL,
                        revealed_at TEXT
                    );
                    CREATE TABLE blind_trials (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES blind_sessions(id) ON DELETE CASCADE,
                        case_id TEXT NOT NULL,
                        case_order INTEGER NOT NULL,
                        expected_text TEXT NOT NULL,
                        left_item_id TEXT NOT NULL REFERENCES items(id),
                        right_item_id TEXT NOT NULL REFERENCES items(id),
                        UNIQUE(session_id, case_id, case_order)
                    );
                    CREATE TABLE ratings (
                        trial_id TEXT PRIMARY KEY REFERENCES blind_trials(id) ON DELETE CASCADE,
                        intelligibility INTEGER NOT NULL CHECK(intelligibility BETWEEN 1 AND 5),
                        identity_consistency INTEGER NOT NULL CHECK(identity_consistency BETWEEN 1 AND 5),
                        acting_fit INTEGER NOT NULL CHECK(acting_fit BETWEEN 1 AND 5),
                        naturalness INTEGER NOT NULL CHECK(naturalness BETWEEN 1 AND 5),
                        artifacts INTEGER NOT NULL CHECK(artifacts BETWEEN 1 AND 5),
                        notes TEXT NOT NULL DEFAULT '',
                        saved_at TEXT NOT NULL
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            if version < 2:
                # Keep every original pair-level rating verbatim. There is no
                # defensible way to infer two speaker scores from one pair score.
                score_columns = ",\n".join(
                    f"{side}_{field} INTEGER NOT NULL CHECK({side}_{field} BETWEEN 1 AND 5)"
                    for side in ("left", "right") for field in RATING_FIELDS)
                db.executescript(f"""
                    BEGIN IMMEDIATE;
                    CREATE TABLE side_ratings (
                        trial_id TEXT PRIMARY KEY REFERENCES blind_trials(id) ON DELETE CASCADE,
                        {score_columns},
                        preference TEXT CHECK(preference IN ('A','B','tie')),
                        notes TEXT NOT NULL DEFAULT '',
                        saved_at TEXT NOT NULL
                    );
                    PRAGMA user_version=2;
                    COMMIT;
                """)
            if version < 3:
                self._rename_comparisons(db, existing=version > 0)

    def _rename_comparisons(self, db, *, existing):
        """Schema 3: the audio of comparisons lives in out/comparisons, which earlier builds called out/m5. A database that
        already existed gets the folder moved and the paths it keeps rewritten to match; a new one has nothing to move.
        The schema number changes last, in the same transaction as the rewrite, so a stop at any point leaves something the
        next open finishes. If the folder cannot be moved the database stays at schema 2 and the next open tries again."""
        if existing:
            old, new = self.out_root / LEGACY_COMPARISONS_DIR, self.out_root / COMPARISONS_DIR
            try:
                if move_folder(old, new):
                    self.migration_notes.append(f"moved out/{LEGACY_COMPARISONS_DIR} to out/{COMPARISONS_DIR}")
            except FolderMoveError as exc:
                self.migration_notes.append(f"did not move out/{LEGACY_COMPARISONS_DIR} to out/{COMPARISONS_DIR}: {exc}")
                return
            if _contained(self.out_root, self.lab_root):
                before, after = (_relative(folder, self.lab_root) + "/" for folder in (old, new))
                for table, column in (("runs", "manifest_path"), ("items", "raw_path"), ("items", "listening_path")):
                    db.execute(f"UPDATE {table} SET {column}=? || substr({column}, ?) WHERE substr({column}, 1, ?)=?",
                               (after, len(before) + 1, len(before), before))
        db.execute("PRAGMA user_version=3")

    def register_run_manifest(self, manifest_path):
        manifest_path = Path(manifest_path).resolve()
        comparisons_root = (self.out_root / COMPARISONS_DIR).resolve()
        if not _contained(manifest_path, comparisons_root) or not manifest_path.is_file() or manifest_path.suffix.lower() != ".json":
            raise ValueError(f"Evaluation manifest must be a JSON file under voice-lab/out/{COMPARISONS_DIR}/.")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("Unsupported evaluation run manifest schema.")
        supplied = raw.get("checksum")
        payload = {key: value for key, value in raw.items() if key != "checksum"}
        checksum = digest_json(payload)
        if supplied != checksum:
            raise ValueError("Evaluation run manifest checksum does not match its contents.")
        run_id, suite_id = raw.get("run_id"), raw.get("suite_id")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 80 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in run_id):
            raise ValueError("Evaluation run ID contains unsupported characters.")
        samples = raw.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ValueError("Evaluation run must contain at least one sample record.")
        normalized = []
        seen = set()
        run_dir = manifest_path.parent.resolve()
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ValueError(f"Evaluation sample {index + 1} must be an object.")
            case_id, engine_id, repetition = sample.get("case_id"), sample.get("engine_id"), sample.get("repetition")
            if not isinstance(case_id, str) or not case_id or not isinstance(engine_id, str) or not engine_id:
                raise ValueError(f"Evaluation sample {index + 1} is missing its case or engine ID.")
            if isinstance(repetition, bool) or not isinstance(repetition, int) or repetition < 0:
                raise ValueError(f"Evaluation sample {index + 1} has an invalid repetition number.")
            unique = (case_id, repetition, engine_id)
            if unique in seen:
                raise ValueError(f"Duplicate evaluation sample for {unique}.")
            seen.add(unique)
            text = sample.get("expected_text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Evaluation sample {index + 1} needs expected_text.")
            paths = {}
            for kind in ("raw", "listening"):
                relative = sample.get(f"{kind}_path")
                expected_hash = sample.get(f"{kind}_sha256")
                if not isinstance(relative, str) or Path(relative).is_absolute():
                    raise ValueError(f"Evaluation sample {index + 1} needs a relative {kind} audio path.")
                target = (run_dir / relative).resolve()
                if not _contained(target, run_dir) or not target.is_file():
                    raise ValueError(f"Evaluation sample {index + 1} {kind} path escapes the run or does not exist.")
                actual_hash = digest_bytes(target.read_bytes())
                if actual_hash != expected_hash:
                    raise ValueError(f"Evaluation sample {index + 1} {kind} audio checksum mismatch.")
                paths[kind] = (target, actual_hash)
            item_id = sample.get("item_id")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 100:
                raise ValueError(f"Evaluation sample {index + 1} needs an opaque item_id.")
            normalized.append((item_id, sample, paths))
        with self._connect() as db:
            existing = db.execute("SELECT id,manifest_sha256 FROM runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                if existing["manifest_sha256"] == checksum:
                    return self.get_run(run_id)
                raise ValueError("Evaluation run ID already exists with different contents.")
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                       (run_id, str(raw.get("title", suite_id or run_id)), str(suite_id or "unknown"),
                        int(raw.get("suite_version", 1)), str(raw.get("created_at", _now())),
                        _relative(manifest_path, self.lab_root), checksum,
                        json.dumps(raw.get("summary", {}), ensure_ascii=False, sort_keys=True),
                        json.dumps(raw, ensure_ascii=False, sort_keys=True)))
            for item_id, sample, paths in normalized:
                db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    item_id, run_id, sample["case_id"], sample["repetition"], sample["engine_id"],
                    sample["expected_text"], _relative(paths["raw"][0], self.lab_root), paths["raw"][1],
                    _relative(paths["listening"][0], self.lab_root), paths["listening"][1],
                    json.dumps(sample.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(sample.get("diagnostics", {}), ensure_ascii=False, sort_keys=True),
                    "{}", _now()))
        return self.get_run(run_id)

    def list_runs(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,title,suite_id,suite_version,created_at,manifest_path,manifest_sha256,summary_json "
                              "FROM runs ORDER BY created_at DESC,id").fetchall()
        return [{**{key: row[key] for key in row.keys() if key != "summary_json"},
                 "summary": json.loads(row["summary_json"])} for row in rows]

    def list_blind_sessions(self):
        with self._connect() as db:
            rows = db.execute("SELECT s.id,s.run_id,s.evaluator,s.state,s.created_at,s.revealed_at,r.title "
                              "FROM blind_sessions s JOIN runs r ON r.id=s.run_id "
                              "ORDER BY s.created_at DESC,s.id LIMIT 100").fetchall()
            counts = {row["id"]: row["count"] for row in db.execute(
                "SELECT t.session_id AS id,COUNT(*) AS count FROM blind_trials t GROUP BY t.session_id")}
            ratings = {row["id"]: row["count"] for row in db.execute(
                "SELECT t.session_id AS id,COUNT(*) AS count FROM blind_trials t JOIN side_ratings r ON r.trial_id=t.id GROUP BY t.session_id")}
            legacy = {row["id"]: row["count"] for row in db.execute(
                "SELECT t.session_id AS id,COUNT(*) AS count FROM blind_trials t JOIN ratings r ON r.trial_id=t.id GROUP BY t.session_id")}
        return [{**dict(row), "trial_count": counts.get(row["id"], 0),
                 "rated_count": ratings.get(row["id"], 0), "legacy_rated_count": legacy.get(row["id"], 0)} for row in rows]

    def get_run(self, run_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (str(run_id),)).fetchone()
            items = db.execute("SELECT * FROM items WHERE run_id=? ORDER BY case_id,repetition,engine_id",
                               (str(run_id),)).fetchall()
        if row is None:
            raise KeyError("Evaluation run was not found.")
        result = {key: row[key] for key in row.keys() if key not in {"summary_json", "manifest_json"}}
        result["summary"] = json.loads(row["summary_json"])
        result["manifest"] = json.loads(row["manifest_json"])
        result["samples"] = []
        for item in items:
            sample = {key: item[key] for key in item.keys() if key not in {"metadata_json", "diagnostics_json", "transcript_json"}}
            sample["metadata"] = json.loads(item["metadata_json"])
            sample["diagnostics"] = json.loads(item["diagnostics_json"])
            sample["transcript"] = json.loads(item["transcript_json"])
            result["samples"].append(sample)
        return result

    def create_blind_session(self, run_id, evaluator="local listener"):
        evaluator = str(evaluator or "local listener").strip()
        if not evaluator or len(evaluator) > 120:
            raise ValueError("Evaluator label must contain 1–120 characters.")
        with self._connect() as db:
            run = db.execute("SELECT id FROM runs WHERE id=?", (str(run_id),)).fetchone()
            if run is None:
                raise KeyError("Evaluation run was not found.")
            items = db.execute("SELECT id,case_id,repetition,engine_id,expected_text FROM items "
                               "WHERE run_id=? ORDER BY case_id,repetition,engine_id", (str(run_id),)).fetchall()
            prior = db.execute("SELECT li.engine_id AS left_engine,ri.engine_id AS right_engine "
                "FROM blind_trials t JOIN blind_sessions s ON s.id=t.session_id "
                "JOIN items li ON li.id=t.left_item_id JOIN items ri ON ri.id=t.right_item_id "
                "WHERE s.run_id=?", (str(run_id),)).fetchall()
            pair_left_counts = {}
            for row in prior:
                pair = tuple(sorted((row["left_engine"], row["right_engine"])))
                counts = pair_left_counts.setdefault(pair, {})
                counts[row["left_engine"]] = counts.get(row["left_engine"], 0) + 1
            grouped = {}
            for item in items:
                grouped.setdefault((item["case_id"], item["repetition"]), {})[item["engine_id"]] = item
            common = [(key, engines) for key, engines in grouped.items() if len(engines) > 1]
            first_repetition = min((key[1] for key, _ in common), default=None)
            common = [(key, engines) for key, engines in common if key[1] == first_repetition]
            if not common:
                raise ValueError("Run has no case with two or more models at a common repetition.")
            session_id = secrets.token_hex(16)
            db.execute("INSERT INTO blind_sessions VALUES(?,?,?,?,?,NULL)",
                       (session_id, str(run_id), evaluator, "blind", _now()))
            session_left_counts = {}
            pairs_by_case = []
            for (case_id, repetition), engines in sorted(common, key=lambda item: (item[0][0], item[0][1])):
                for left_engine, right_engine in itertools.combinations(sorted(engines), 2):
                    pairs_by_case.append((case_id, repetition, left_engine, right_engine,
                                          engines[left_engine], engines[right_engine]))
            for case_order, (case_id, repetition, engine_a, engine_b, item_a, item_b) in enumerate(pairs_by_case):
                pair = tuple(sorted((engine_a, engine_b)))
                cumulative = pair_left_counts.setdefault(pair, {})
                current = session_left_counts.setdefault(pair, {})
                count_a, count_b = cumulative.get(engine_a, 0), cumulative.get(engine_b, 0)
                session_a, session_b = current.get(engine_a, 0), current.get(engine_b, 0)
                if session_a != session_b:
                    choose_a_left = session_a < session_b
                elif count_a == count_b:
                    choose_a_left = bool(secrets.randbelow(2))
                else:
                    choose_a_left = count_a < count_b
                left, right = (item_a, item_b) if choose_a_left else (item_b, item_a)
                cumulative[left["engine_id"]] = cumulative.get(left["engine_id"], 0) + 1
                current[left["engine_id"]] = current.get(left["engine_id"], 0) + 1
                trial_id = secrets.token_hex(16)
                db.execute("INSERT INTO blind_trials VALUES(?,?,?,?,?,?,?)",
                           (trial_id, session_id, case_id, case_order, item_a["expected_text"], left["id"], right["id"]))
        return self.get_blind_session(session_id)

    def _item_public_url(self, item_id):
        return f"/api/evaluations/blind-audio/{item_id}"

    def get_blind_audio_path(self, item_id):
        with self._connect() as db:
            row = db.execute("SELECT listening_path FROM items WHERE id=?", (str(item_id),)).fetchone()
        if row is None:
            raise KeyError("Blind evaluation audio was not found.")
        target = (self.lab_root / row[0]).resolve()
        comparisons_root = (self.out_root / COMPARISONS_DIR).resolve()
        if not _contained(target, comparisons_root) or not target.is_file():
            raise FileNotFoundError(f"Blind audition copy is missing or outside out/{COMPARISONS_DIR}.")
        return target

    def get_blind_session(self, session_id):
        with self._connect() as db:
            session = db.execute("SELECT * FROM blind_sessions WHERE id=?", (str(session_id),)).fetchone()
            trials = db.execute("SELECT t.*,r.intelligibility,r.identity_consistency,r.acting_fit,r.naturalness,r.artifacts,r.notes,r.saved_at "
                                "FROM blind_trials t LEFT JOIN ratings r ON r.trial_id=t.id "
                                "WHERE t.session_id=? ORDER BY t.case_order", (str(session_id),)).fetchall()
            side_ratings = {row["trial_id"]: row for row in db.execute(
                "SELECT r.* FROM side_ratings r JOIN blind_trials t ON t.id=r.trial_id WHERE t.session_id=?",
                (str(session_id),))}
            revealed = []
            if session and session["state"] == "revealed":
                revealed = db.execute("SELECT t.id AS trial_id,li.engine_id AS left_engine,ri.engine_id AS right_engine "
                    "FROM blind_trials t JOIN items li ON li.id=t.left_item_id JOIN items ri ON ri.id=t.right_item_id "
                    "WHERE t.session_id=? ORDER BY t.case_order", (str(session_id),)).fetchall()
        if session is None:
            raise KeyError("Blind evaluation session was not found.")
        reveal_map = {row["trial_id"]: {"left_engine": row["left_engine"], "right_engine": row["right_engine"]}
                      for row in revealed}
        result = {"session_id": session["id"], "run_id": session["run_id"], "evaluator": session["evaluator"],
                  "state": session["state"], "created_at": session["created_at"], "revealed_at": session["revealed_at"],
                  "rating_schema_version": 2, "trials": []}
        for row in trials:
            trial = {"trial_id": row["id"], "case_id": row["case_id"], "text": row["expected_text"],
                     "left": {"label": "A", "audio_url": self._item_public_url(row["left_item_id"])},
                     "right": {"label": "B", "audio_url": self._item_public_url(row["right_item_id"])},
                     "rating": None, "legacy_rating": None, "rating_complete": False, "rating_format": None}
            if row["saved_at"]:
                trial["rating"] = {field: row[field] for field in RATING_FIELDS}
                trial["rating"].update({"notes": row["notes"], "saved_at": row["saved_at"]})
                trial["legacy_rating"] = dict(trial["rating"])
                trial["rating_format"] = "legacy-pair"
            side_rating = side_ratings.get(row["id"])
            if side_rating:
                trial["rating"] = {
                    side: {field: side_rating[f"{side}_{field}"] for field in RATING_FIELDS}
                    for side in ("left", "right")}
                trial["rating"].update({"preference": side_rating["preference"],
                    "notes": side_rating["notes"], "saved_at": side_rating["saved_at"]})
                trial["rating_format"] = "per-side"
                trial["rating_complete"] = True
            if session["state"] == "revealed":
                trial["revealed"] = reveal_map[row["id"]]
            result["trials"].append(trial)
        return result

    def save_rating(self, session_id, trial_id, scores, notes=""):
        legacy = isinstance(scores, dict) and set(scores) == set(RATING_FIELDS)
        if legacy:
            normalized = _rating_scores(scores)
        else:
            if (not isinstance(scores, dict) or not {"left", "right"}.issubset(scores) or
                    set(scores) - {"left", "right", "preference"}):
                raise ValueError("Save separate left (A) and right (B) scores, with an optional A, B or tie preference.")
            normalized = {side: _rating_scores(scores[side]) for side in ("left", "right")}
            preference = scores.get("preference")
            if preference is not None and preference not in ("A", "B", "tie"):
                raise ValueError("Preference must be A, B, tie or null.")
        notes = str(notes or "")
        if len(notes) > 2000:
            raise ValueError("Rating notes must be 2000 characters or fewer.")
        with self._connect() as db:
            row = db.execute("SELECT s.state FROM blind_trials t JOIN blind_sessions s ON s.id=t.session_id "
                             "WHERE t.id=? AND t.session_id=?", (str(trial_id), str(session_id))).fetchone()
            if row is None:
                raise KeyError("Blind trial does not belong to this session.")
            if row["state"] != "blind":
                raise ValueError("Ratings are locked after the model mapping is revealed.")
            if not legacy:
                columns = [f"{side}_{field}" for side in ("left", "right") for field in RATING_FIELDS]
                values = [normalized[side][field] for side in ("left", "right") for field in RATING_FIELDS]
                all_columns = ["trial_id", *columns, "preference", "notes", "saved_at"]
                updates = ",".join(f"{column}=excluded.{column}" for column in all_columns[1:])
                db.execute(f"INSERT INTO side_ratings({','.join(all_columns)}) "
                           f"VALUES({','.join('?' for _ in all_columns)}) "
                           f"ON CONFLICT(trial_id) DO UPDATE SET {updates}",
                           (str(trial_id), *values, preference, notes, _now()))
            else:
                db.execute("""INSERT INTO ratings(trial_id,intelligibility,identity_consistency,acting_fit,naturalness,artifacts,notes,saved_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(trial_id) DO UPDATE SET
                intelligibility=excluded.intelligibility,identity_consistency=excluded.identity_consistency,
                acting_fit=excluded.acting_fit,naturalness=excluded.naturalness,artifacts=excluded.artifacts,
                notes=excluded.notes,saved_at=excluded.saved_at""",
                (str(trial_id), normalized["intelligibility"], normalized["identity_consistency"],
                 normalized["acting_fit"], normalized["naturalness"], normalized["artifacts"], notes, _now()))
        return self.get_blind_session(session_id)

    def reveal_blind_session(self, session_id):
        with self._connect() as db:
            session = db.execute("SELECT state FROM blind_sessions WHERE id=?", (str(session_id),)).fetchone()
            if session is None:
                raise KeyError("Blind evaluation session was not found.")
            if session["state"] == "blind":
                counts = db.execute("SELECT (SELECT COUNT(*) FROM blind_trials WHERE session_id=?), "
                    "(SELECT COUNT(*) FROM side_ratings r JOIN blind_trials t ON t.id=r.trial_id WHERE t.session_id=?)",
                    (str(session_id), str(session_id))).fetchone()
                if counts[0] != counts[1]:
                    raise ValueError(f"Rate every blind trial for both A and B before revealing the mapping ({counts[1]}/{counts[0]} rated). Legacy pair-level scores do not count as per-side ratings.")
                db.execute("UPDATE blind_sessions SET state='revealed',revealed_at=? WHERE id=?", (_now(), str(session_id)))
        return self.get_blind_session(session_id)

    def add_transcript(self, item_id, heard_text, evaluator):
        with self._connect() as db:
            row = db.execute("SELECT expected_text,raw_path,diagnostics_json FROM items WHERE id=?", (str(item_id),)).fetchone()
            if row is None:
                raise KeyError("Evaluation sample was not found.")
            audio_path = (self.lab_root / row["raw_path"]).resolve()
            current = json.loads(row["diagnostics_json"])
            transcript = compare_transcript(row["expected_text"], heard_text, evaluator)
            current["transcript"] = transcript
            current["flags"] = list(dict.fromkeys(current.get("flags", []) + transcript.get("flags", [])))
            db.execute("UPDATE items SET transcript_json=?,diagnostics_json=? WHERE id=?",
                       (json.dumps(transcript, ensure_ascii=False, sort_keys=True),
                        json.dumps(current, ensure_ascii=False, sort_keys=True), str(item_id)))
        return {"item_id": str(item_id), "diagnostics": analyze_audio(audio_path, expected_text=row["expected_text"],
                heard_text=heard_text, evaluator=evaluator)}

    def get_item(self, item_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (str(item_id),)).fetchone()
        if row is None:
            raise KeyError("Evaluation sample was not found.")
        result = dict(row)
        for field, target in (("metadata_json", "metadata"), ("diagnostics_json", "diagnostics"),
                              ("transcript_json", "transcript")):
            result[target] = json.loads(result.pop(field))
        return result
