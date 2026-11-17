"""Durable dubbing exports and local watch-folder intake."""

from functools import partial
import json
from pathlib import Path
import re
import threading

import dubbing
from scene_store import digest_json, utc_now

ID = re.compile(r"[a-f0-9]{32}")
ACTIVE = {"queued", "waiting_for_takes", "publishing"}
WATCH_SUFFIX = ".dubjob.json"
WATCH_LIMIT = 64 * 1024


def _fingerprint(scene):
    """Cue edits and settings change the production, while assigning takes does not."""
    production = {key: value for key, value in scene.items() if key not in {"revision", "checksum", "lines"}}
    production["lines"] = [
        {key: value for key, value in line.items() if key not in {"selected_take_id", "selected_takes"}}
        for line in scene["lines"]
    ]
    return digest_json(production)


def _file_stamp(path):
    if not path.is_file() or path.is_symlink() or path.is_junction():
        raise ValueError("Use regular files within the selected watch folder.")
    info = path.stat()
    return info.st_size, info.st_mtime_ns


class DubQueue:
    def __init__(self, scenes, batches):
        self.scenes, self.batches = scenes, batches
        self.root = scenes.root / "out" / "dubs" / "queue"
        self.lock = threading.RLock()
        self.changed = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.observed = {}
        self.handled = {}

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="voice-lab-dubbing", daemon=True)
            self.thread.start()

    def close(self):
        self.stopping.set()
        self.changed.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _loop(self):
        while not self.stopping.is_set():
            self.changed.clear()
            self.tick()
            self.changed.wait(2)

    def _list(self, section):
        folder = self.root / section
        rows = []
        for path in folder.glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                if row["id"] == path.stem and ID.fullmatch(path.stem):
                    rows.append(row)
            except (OSError, ValueError, KeyError):
                continue
        return rows

    def _write(self, section, row):
        folder = self.root / section
        folder.mkdir(parents=True, exist_ok=True)
        dubbing.write_json_atomic(folder / (row["id"] + ".json"), row)

    def _get(self, section, ident):
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise ValueError("Invalid dubbing queue ID.")
        try:
            return json.loads((self.root / section / (ident + ".json")).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError("Dubbing queue record not found.") from exc

    def snapshot(self):
        with self.lock:
            return {
                "jobs": sorted(self._list("jobs"), key=lambda row: (row["created"], row["id"]), reverse=True)[:100],
                "watches": self._list("dubbing-watches"),
            }

    def _plan(self, item, origin=None):
        from dub_translation import locale

        if not isinstance(item, dict) or not isinstance(item.get("export"), dict):
            raise ValueError("Each batch item needs a scene ID and export options.")
        scene_id = item.get("scene_id")
        _, scene = dubbing._directory(self.scenes, scene_id)
        options = dict(item["export"])
        if type(options.get("expected_revision")) is not int or options["expected_revision"] != scene["revision"]:
            raise ValueError("Refresh the saved scene revision before queuing an export.")
        options["target_language"] = locale(options.get("target_language"))
        render = item.get("render_missing", False)
        if type(render) is not bool:
            raise ValueError("render_missing must be true or false.")
        fingerprint = _fingerprint(scene)
        key = digest_json([scene_id, fingerprint, scene["revision"], options, render, origin])[:32]
        return {
            "schema_version": 1,
            "id": key,
            "scene_id": scene_id,
            "scene": scene["name"],
            "script_checksum": fingerprint,
            "export": options,
            "render_missing": render,
            "media": origin,
            "scene_job_id": digest_json([key, "takes"])[:32],
            "created": utc_now(),
            "status": "queued",
            "error": "",
        }

    def enqueue(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 40:
            raise ValueError("Queue 1–40 dubbing deliveries at once.")
        with self.lock:
            plans = [self._plan(item) for item in items]
            jobs = [self._enqueue(plan) for plan in plans]
        self.changed.set()
        return jobs

    def _enqueue(self, plan):
        try:
            return self._get("jobs", plan["id"])
        except KeyError:
            self._write("jobs", plan)
            return plan

    def add_watch(self, value):
        if not isinstance(value, str):
            raise ValueError("Enter a local absolute folder path.")
        root = Path(value)
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")) or not root.is_dir():
            raise ValueError("Choose an existing local folder.")
        root = root.resolve(strict=True)
        if root.drive.startswith(chr(92) * 2):
            raise ValueError("Choose a local folder rather than a UNC share.")
        record = {"id": digest_json(str(root).casefold())[:32], "path": str(root), "error": "", "enabled": True}
        with self.lock:
            self._write("dubbing-watches", record)
        self.changed.set()
        return record

    def remove_watch(self, ident):
        with self.lock:
            row = self._get("dubbing-watches", ident)
            row["enabled"] = False
            self._write("dubbing-watches", row)
        return row

    def action(self, ident, action):
        with self.lock:
            row = self._get("jobs", ident)
            if action == "cancel" and row["status"] in {"queued", "waiting_for_takes"}:
                if row["status"] == "waiting_for_takes":
                    self.scenes.cancel(row["scene_job_id"])
                row.update(status="cancelled", error="")
            elif action == "retry" and row["status"] in {"failed", "cancelled"}:
                try:
                    scene_job = self.scenes.get_job(row["scene_job_id"])
                except KeyError:
                    scene_job = None
                if scene_job and scene_job["status"] == "failed":
                    self.scenes.retry_failed(row["scene_job_id"])
                    self.batches.wake()
                elif scene_job and scene_job["status"] in {"cancelled", "cancelling"}:
                    if scene_job["status"] == "cancelling":
                        raise ValueError("Wait until the selected scene render finishes cancelling.")
                    row["scene_job_id"] = digest_json([row["scene_job_id"], "retry"])[:32]
                row.update(status="queued", error="")
            else:
                raise ValueError("Cancel a queued or rendering delivery, or retry a failed or cancelled delivery.")
            self._write("jobs", row)
        self.changed.set()
        return row

    def _scan(self, watch):
        root = Path(watch["path"])
        if root.resolve(strict=True) != root:
            raise ValueError("The watch folder now points to a different location.")
        errors = []
        for descriptor in sorted(root.glob("*" + WATCH_SUFFIX))[:128]:
            try:
                self._ingest(watch, root, descriptor)
            except (KeyError, OSError, ValueError) as exc:
                errors.append(f"{descriptor.name}: {exc}")
        if errors:
            raise ValueError("; ".join(errors[:3]))

    def _ingest(self, watch, root, descriptor):
        stamp = _file_stamp(descriptor)
        key = str(descriptor)
        if stamp[0] > WATCH_LIMIT:
            raise ValueError("The manifest exceeds 64 KiB.")
        if self.observed.get(key) != stamp:
            self.observed[key] = stamp
            return
        data = descriptor.read_bytes()
        if _file_stamp(descriptor) != stamp:
            return
        item = json.loads(data.decode("utf-8-sig"))
        filename = item.get("media") if isinstance(item, dict) else None
        origin = None
        if filename is not None:
            if (
                not isinstance(filename, str)
                or Path(filename).name != filename
                or any(ch in filename for ch in ("/", chr(92), ":"))
            ):
                raise ValueError("Name the source file in this folder without a directory.")
            media = root / filename
            if media.suffix.lower() not in dubbing.FORMATS:
                raise ValueError("The manifest names an unsupported media type.")
            if not media.exists():
                return
            mstamp = _file_stamp(media)
            pair = (key, filename)
            if self.observed.get(pair) != mstamp:
                self.observed[pair] = mstamp
                return
            if self.handled.get(key) == (stamp, mstamp):
                return
            if mstamp[0] > dubbing.MAX_MEDIA_BYTES:
                raise ValueError("The media exceeds the upload size limit.")
            digest = dubbing.sha256(media)
            if _file_stamp(media) != mstamp:
                return
            origin = {"watch_id": watch["id"], "name": filename, "sha256": digest}
        else:
            mstamp = None
            if self.handled.get(key) == (stamp, None):
                return
        marker = digest_json([watch["id"], item, origin])[:32]
        with self.lock:
            if not self._get("dubbing-watches", watch["id"])["enabled"]:
                return
            try:
                self._get("seen", marker)
            except KeyError:
                self._enqueue(self._plan(item, origin))
                self._write("seen", {"id": marker})
            self.handled[key] = (stamp, mstamp)

    def tick(self):
        with self.lock:
            watches = [row for row in self._list("dubbing-watches") if row["enabled"]]
        for watch in watches:
            try:
                self._scan(watch)
            except Exception as exc:
                message = str(exc)
            else:
                message = ""
            if watch["error"] != message:
                with self.lock:
                    latest = self._get("dubbing-watches", watch["id"])
                    latest["error"] = message
                    self._write("dubbing-watches", latest)
        with self.lock:
            jobs = sorted(
                (row for row in self._list("jobs") if row["status"] in ACTIVE), key=lambda row: (row["created"], row["id"])
            )
        for row in jobs:
            if self.stopping.is_set():
                return
            self._advance(row)

    def _attach_source(self, row):
        origin = row["media"]
        if not origin:
            return
        watch = self._get("dubbing-watches", origin["watch_id"])
        root = Path(watch["path"])
        if root.resolve(strict=True) != root:
            raise ValueError("The watch folder no longer points to its selected path.")
        source = root / origin["name"]
        length, _ = _file_stamp(source)
        with source.open("rb") as stream:
            dubbing.attach(self.scenes, row["scene_id"], stream, length, source.name, expected_source_sha=origin["sha256"])

    def _prepare(self, row):
        scene = self.scenes.get_scene(row["scene_id"])
        if _fingerprint(scene) != row["script_checksum"]:
            raise ValueError("The scene's script changed. Queue the newly saved revision.")
        if row["status"] == "queued":
            self._attach_source(row)
        missing = [
            line["line_id"] + "/" + segment["id"]
            for line in scene["lines"]
            if not line.get("dub", {}).get("mute")
            for segment in line["segments"]
            if not line.get("selected_takes", {}).get(segment["id"])
        ]
        if missing and row["render_missing"]:
            ident = row["scene_job_id"]
            try:
                job = self.scenes.get_job(ident)
            except KeyError:
                job = self.scenes.create_job(scene["scene_id"], segment_ids=missing, persistent_id=ident)
                self.batches.wake()
            if job["status"] in {"failed", "cancelled", "paused"}:
                raise ValueError("The scene render stopped. Review its rows, then retry this delivery.")
            if job["status"] != "completed":
                return "waiting_for_takes", None
            for part in job["rows"]:
                selector = part["line_id"] + "/" + part["segment_id"]
                if part["take_id"] and selector in missing:
                    self.scenes.select_take(scene["scene_id"], part["line_id"], part["take_id"], part["segment_id"])
                    missing.remove(selector)
            if missing:
                raise ValueError("The finished render has missing takes. Review the scene before exporting.")
        scene = self.scenes.get_scene(row["scene_id"])
        if _fingerprint(scene) != row["script_checksum"]:
            raise ValueError("The scene changed before this delivery could be published.")
        return "publishing", partial(
            dubbing.export, self.scenes, scene["scene_id"], {**row["export"], "expected_revision": scene["revision"]}
        )

    def _advance(self, row):
        # Keep a delivery's publish step out of the queue lock. Its manifest is itself atomic and idempotent.
        with self.lock:
            row = self._get("jobs", row["id"])
            if row["status"] not in ACTIVE:
                return
            try:
                state, publish = self._prepare(row)
            except Exception as exc:
                row.update(status="failed", error=str(exc))
                self._write("jobs", row)
                return
            row.update(status=state, error="")
            self._write("jobs", row)
        if publish:
            try:
                delivery = publish()
                row.update(status="complete", error="", delivery_id=delivery["id"])
            except Exception as exc:
                row.update(status="failed", error=str(exc))
            with self.lock:
                self._write("jobs", row)
