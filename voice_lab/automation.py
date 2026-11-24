"""Versioned, resumable recipes. All inputs and outputs belong to the current library."""

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from voice_lab import speech_input
from voice_lab import connector_profiles
from voice_lab.local_ownership import LocalOwnership

SCHEMA = 1
ID = re.compile(r"[a-f0-9]{32}")
ASSET = re.compile(r"[a-f0-9]{64}")
OPERATIONS = {
    "speak": ("text", "audio", ("voice", "speed", "provider")),
    "transcribe": ("audio", "text", ("language",)),
    "translate": ("text", "text", ("source_language", "target_language", "provider")),
    "normalize": ("audio", "audio", ("target_lufs", "ceiling_dbtp", "precision")),
    "convert": ("audio", "audio", ("identity_id",)),
    "dub": ("scene", "dub_job", ("target_language", "format", "render_missing")),
}
PROVIDERS = {
    "speak": "local-voice-lab",
    "transcribe": "local-faster-whisper",
    "translate": "",
    "normalize": "local-mastering",
    "convert": "local-voice-conversion",
    "dub": "local-dub-queue",
}
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, document TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS items (
    run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, state TEXT NOT NULL, next_step INTEGER NOT NULL,
    value TEXT NOT NULL, error TEXT, PRIMARY KEY(run_id, ordinal)
);
CREATE TABLE IF NOT EXISTS step_receipts (
    run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, sequence INTEGER NOT NULL, output TEXT NOT NULL,
    PRIMARY KEY(run_id, ordinal, sequence)
);
CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, suffix TEXT NOT NULL, bytes INTEGER NOT NULL);
"""


def _document(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid automation ID.")
    return value


def recipe(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA:
        raise ValueError("Use automation recipe schema version 1.")
    name, initial, steps = value.get("name"), value.get("input_kind"), value.get("steps")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("Give the recipe a name of up to 100 characters.")
    if initial not in ("text", "audio", "scene") or not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise ValueError("Choose an input type and 1–8 steps.")
    plan, results = [], [initial]
    for sequence, row in enumerate(steps):
        if not isinstance(row, dict) or not isinstance(row.get("operation"), str) or row["operation"] not in OPERATIONS:
            raise ValueError("Choose a supported recipe operation.")
        operation, options = row["operation"], row.get("settings", {})
        expected, result, fields = OPERATIONS[operation]
        source_step = row.get("source_step", sequence)
        if type(source_step) is not int or not 0 <= source_step <= sequence:
            raise ValueError("A step must start from the original input or an earlier step.")
        kind = results[source_step]
        if expected != kind:
            raise ValueError(f"{operation} needs {expected} input. The selected source provides {kind}.")
        if (
            not isinstance(options, dict)
            or set(options) - set(fields)
            or any(
                not isinstance(value, (str, bool, int, float))
                or (isinstance(value, str) and len(value) > 160)
                or (isinstance(value, float) and not math.isfinite(value))
                for value in options.values()
            )
        ):
            raise ValueError(f"Invalid {operation} settings.")
        if operation == "speak" and not isinstance(options.get("voice"), str):
            raise ValueError("Choose a Voice Lab voice name for speech.")
        if operation == "convert":
            identity_id = options.get("identity_id")
            if not isinstance(identity_id, str) or not ID.fullmatch(identity_id):
                raise ValueError("Choose an approved Cast voice with a recording.")
        if operation == "translate" and (
            not isinstance(options.get("source_language"), str) or not isinstance(options.get("target_language"), str)
        ):
            raise ValueError("Choose source and target languages and a translation provider.")
        if operation == "dub" and not isinstance(options.get("target_language"), str):
            raise ValueError("Choose a target language for dubbing.")
        provider = options.get("provider", PROVIDERS[operation])
        builtin = {PROVIDERS[operation]}
        custom = None
        if not isinstance(provider, str):
            raise ValueError("Choose a configured provider ID.")
        if operation == "translate" and not provider:
            raise ValueError("Add a translation provider in Automation and choose it for this step.")
        if provider not in builtin:
            custom = connector_profiles.public(connector_profiles.get(provider, operation))
            prior = row.get("connector")
            if prior is not None and (not isinstance(prior, dict) or prior.get("revision") != custom["revision"]):
                raise ValueError("The provider settings changed. Review the recipe preview again.")
        plan.append(
            {
                "operation": operation,
                "settings": options,
                "source_step": source_step,
                "input_kind": kind,
                "output_kind": result,
                "provider": provider,
                "connector": custom,
                "transfer": ("text, voice and model" if operation == "speak" else "text and language pair") if custom else None,
                "destination": custom["destination"] if custom else "dub queue" if operation == "dub" else "this computer",
            }
        )
        results.append(result)
    return {"schema_version": SCHEMA, "name": name.strip(), "input_kind": initial, "output_kind": results[-1], "steps": plan}


class Store:
    def __init__(self, root):
        self.root = Path(root) / "data" / "automation"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "jobs.sqlite3"
        self.assets = self.root / "assets"
        self.assets.mkdir(exist_ok=True)
        self.writes = threading.Lock()
        with closing(self.connect()) as db, db:
            db.executescript(SCHEMA_SQL)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=8)
        db.row_factory = sqlite3.Row
        return db

    def save(self, payload):
        clean = recipe(payload)
        supplied = payload.get("id")
        with closing(self.connect()) as db, db:
            if supplied is None:
                ident, rev = uuid.uuid4().hex, 1
                db.execute("INSERT INTO recipes VALUES(?,?,?)", (ident, rev, _document(clean)))
            else:
                ident = _id(supplied)
                wanted = payload.get("revision")
                if type(wanted) is not int or wanted < 1:
                    raise ValueError("Supply the saved recipe revision before editing.")
                rev = wanted + 1
                row = db.execute(
                    "UPDATE recipes SET revision=?,document=? WHERE id=? AND revision=?", (rev, _document(clean), ident, wanted)
                )
                if row.rowcount != 1:
                    raise ValueError("Reload the recipe. Its saved revision changed.")
        return {**clean, "id": ident, "revision": rev}

    def recipes(self):
        with closing(self.connect()) as db:
            return [
                {**json.loads(row["document"]), "id": row["id"], "revision": row["revision"]}
                for row in db.execute("SELECT * FROM recipes ORDER BY rowid DESC LIMIT 200")
            ]

    def put(self, raw, suffix):
        if suffix not in speech_input.FORMATS or not 0 < len(raw) <= speech_input.LIMIT:
            raise ValueError("Automation audio assets are limited to 25 MiB.")
        ident = hashlib.sha256(raw).hexdigest()
        with self.writes, closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM assets WHERE id=?", (ident,)).fetchone()
            if row is None:
                target = self.assets / (ident + suffix)
                scratch = self.assets / (uuid.uuid4().hex + ".partial")
                try:
                    scratch.write_bytes(raw)
                    scratch.replace(target)
                finally:
                    scratch.unlink(missing_ok=True)
                db.execute("INSERT INTO assets VALUES(?,?,?)", (ident, suffix, len(raw)))
            elif row["suffix"] != suffix:
                raise ValueError("These bytes are already saved under another audio format.")
        return ident

    def asset(self, ident):
        if not isinstance(ident, str) or not ASSET.fullmatch(ident):
            raise ValueError("Invalid automation asset ID.")
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError("This automation audio was not uploaded.")
        path = self.assets / (ident + row["suffix"])
        if not path.is_file():
            raise KeyError("The automation file is missing.")
        return path

    def inputs(self, kind, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise ValueError("Choose 1–50 inputs for this run.")
        ready = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Each input needs text, an automation audio ID or a saved scene ID.")
            if kind == "audio":
                self.asset(value)
            elif kind == "scene":
                _id(value)
            elif len(value) > 10_000:
                raise ValueError("Each text input can have up to 10,000 characters.")
            ready.append({"kind": kind, "value": value})
        return ready

    def plan(self, payload):
        if "recipe_id" in payload:
            wanted = _id(payload["recipe_id"])
            with closing(self.connect()) as db:
                row = db.execute("SELECT * FROM recipes WHERE id=?", (wanted,)).fetchone()
            if row is None:
                raise KeyError("Automation recipe not found.")
            document = {**json.loads(row["document"]), "id": wanted, "revision": row["revision"]}
            if payload.get("revision", row["revision"]) != row["revision"]:
                raise ValueError("Reload the recipe. Its saved revision changed.")
        else:
            document = recipe(payload.get("recipe", {}))
        document = {**document, **recipe(document)}
        providers = {}
        for step in document["steps"]:
            if step["transfer"]:
                profile = step["connector"]
                saved = providers.setdefault(profile["revision"], {**profile, "fields": set()})
                saved["fields"].add(step["transfer"])
        providers = [
            {**{key: value for key, value in row.items() if key != "fields"}, "data": " and ".join(sorted(row["fields"]))}
            for row in providers.values()
        ]
        return {
            "recipe": document,
            "inputs": self.inputs(document["input_kind"], payload.get("inputs")),
            "external_transfer": any(row["external"] for row in providers),
            "external_providers": [row for row in providers if row["external"]],
            "provider_approvals": providers,
            "run_location": "data/automation in this library",
        }

    def submit(self, payload):
        plan = self.plan(payload)
        expected = sorted(row["revision"] for row in plan["provider_approvals"])
        chosen = payload.get("approve_provider_revisions", [])
        if not isinstance(chosen, list) or any(not isinstance(value, str) for value in chosen) or sorted(chosen) != expected:
            raise ValueError("Review the preview and explicitly approve each listed provider.")
        if "id" not in plan["recipe"]:
            raise ValueError("Save a named recipe before running it.")
        ident = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO runs(id,document) VALUES(?,?)", (ident, _document(plan["recipe"])))
            db.executemany(
                "INSERT INTO items VALUES(?,?,'queued',0,?,NULL)",
                [(ident, index, _document(item)) for index, item in enumerate(plan["inputs"])],
            )
            db.executemany(
                "INSERT INTO step_receipts VALUES(?,?,0,?)",
                [(ident, index, _document(item)) for index, item in enumerate(plan["inputs"])],
            )
        return self.get(ident)

    def get(self, ident):
        ident = _id(ident)
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            run = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
            if run is None:
                raise KeyError("Automation run not found.")
            items = [
                dict(row)
                for row in db.execute(
                    "SELECT ordinal,state,next_step,value,error FROM items WHERE run_id=? ORDER BY ordinal", (ident,)
                )
            ]
            receipts = {}
            for row in db.execute(
                "SELECT ordinal,sequence,output FROM step_receipts WHERE run_id=? AND sequence>0 ORDER BY sequence", (ident,)
            ):
                receipts.setdefault(row["ordinal"], []).append({"sequence": row["sequence"], **json.loads(row["output"])})
        for item in items:
            item["value"] = json.loads(item["value"])
            item["steps"] = receipts.get(item["ordinal"], [])
        pending = any(item["state"] in ("running", "queued") for item in items)
        failed = sum(item["state"] == "failed" for item in items)
        status = (
            "running" if pending else ("cancelled" if run["cancelled"] else "completed_with_errors" if failed else "completed")
        )
        return {"id": ident, "status": status, "recipe": json.loads(run["document"]), "items": items, "failed": failed}

    def runs(self):
        with closing(self.connect()) as db:
            ids = [row["id"] for row in db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 40")]
        return [self.get(key) for key in ids]

    def cancel(self, ident):
        _id(ident)
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE runs SET cancelled=1 WHERE id=? AND EXISTS(SELECT 1 FROM items WHERE run_id=? AND state IN "
                "('queued','running'))",
                (ident, ident),
            )
            db.execute("UPDATE items SET state='cancelled' WHERE run_id=? AND state='queued'", (ident,))
        return self.get(ident)

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE items SET state=CASE WHEN (SELECT cancelled FROM runs WHERE id=run_id)=1 "
                "THEN 'cancelled' ELSE 'queued' END WHERE state='running'"
            )

    def claim(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT items.*, runs.document FROM items JOIN runs ON id=run_id WHERE state='queued' "
                "AND cancelled=0 ORDER BY runs.rowid, ordinal LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE items SET state='running' WHERE run_id=? AND ordinal=?", (row["run_id"], row["ordinal"]))
            return {**dict(row), "value": json.loads(row["value"]), "document": json.loads(row["document"])}

    def step_input(self, row, step):
        source = step.get("source_step", row["next_step"])
        if source == row["next_step"]:
            return row["value"]
        with closing(self.connect()) as db:
            saved = db.execute(
                "SELECT output FROM step_receipts WHERE run_id=? AND ordinal=? AND sequence=?",
                (row["run_id"], row["ordinal"], source),
            ).fetchone()
        if saved is None:
            raise ValueError("A required earlier recipe output is missing.")
        return json.loads(saved["output"])

    def checkpoint(self, row, state, next_step, value, error=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            tick = db.execute("SELECT cancelled FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            if tick["cancelled"]:
                state = "cancelled"
            prior = db.execute(
                "SELECT error FROM items WHERE run_id=? AND ordinal=?", (row["run_id"], row["ordinal"])
            ).fetchone()["error"]
            if state == "completed" and (prior or error):
                state = "failed"
            error = error or prior
            if next_step > row["next_step"] and state != "cancelled":
                db.execute(
                    "INSERT INTO step_receipts VALUES(?,?,?,?)", (row["run_id"], row["ordinal"], next_step, _document(value))
                )
            db.execute(
                "UPDATE items SET state=?,next_step=?,value=?,error=? WHERE run_id=? AND ordinal=?",
                (state, next_step, _document(value), error, row["run_id"], row["ordinal"]),
            )


class Runner:
    def __init__(self, store, operate):
        self.store, self.operate = store, operate
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.active = None
        self.owner = LocalOwnership(self.store.root / "runner.lock")

    def start(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self.loop, name="voice-lab-automation", daemon=True)
                self.thread.start()
        self.wake.set()

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            if self.active:
                self.active[1].set()
        if self.thread:
            self.thread.join(timeout=3)

    def cancel(self, ident):
        answer = self.store.cancel(ident)
        with self.lock:
            if self.active and self.active[0] == ident:
                self.active[1].set()
        return answer

    def loop(self):
        while not self.stop.is_set():
            if self.owner.acquire():
                try:
                    self.store.recover()
                    while not self.stop.is_set():
                        self.wake.clear()
                        if not self.tick():
                            self.wake.wait(1)
                finally:
                    self.owner.release()
                return
            self.wake.wait(1)
            self.wake.clear()

    def tick(self):
        with self.lock:
            row = self.store.claim()
            if row is None:
                return False
            cancellation = threading.Event()
            self.active = row["run_id"], cancellation
        index, value, steps = row["next_step"], row["value"], row["document"]["steps"]
        state = "completed" if index + 1 == len(steps) else "queued"
        try:
            selected = self.store.step_input(row, steps[index])
            if selected["kind"] in {"error", "blocked"}:
                output = {"kind": "blocked", "error": "Required earlier step failed."}
            else:
                output = self.operate(self.store, steps[index], selected, cancellation)
            self.store.checkpoint(row, state, index + 1, output)
        except Exception as exc:
            if self.stop.is_set():
                self.store.checkpoint(row, "queued", index, value)
            else:
                message = str(exc)[:400]
                self.store.checkpoint(row, state, index + 1, {"kind": "error", "error": message}, message)
        finally:
            with self.lock:
                self.active = None
        return True
