"""Saved manuscripts whose editable chapters use the existing Cast and scene queue."""

import json
import re

from voice_lab import engine_adapters
from voice_lab.book_language import original_line, pronunciations, spoken_line
from voice_lab.manuscript_import import MAX_CHAPTERS, MAX_LINES, MAX_TEXT
from voice_lab.scene_store import (
    DIRECTION_ENGINES,
    MAX_LINE_TEXT,
    MAX_SCENE_LINES,
    _path_lock,
    canonical_json,
    digest_json,
    new_id,
    utc_now,
)

FIELDS = {"title": 120, "author": 160, "narrator": 160, "genre": 80, "description": 2_000, "year": 4}
ACTIVE = {"queued", "running", "pausing", "cancelling"}
HEX = re.compile(r"[a-f0-9]{64}")
MARKUP = re.compile(r"\b\w+(?:['’]\w+)?\b", re.UNICODE)


def metadata(value):
    if not isinstance(value, dict) or set(value) - FIELDS.keys():
        raise ValueError("Use the book title, author, narrator, genre, year and description fields.")
    out = {}
    for key, limit in FIELDS.items():
        text = value.get(key, "")
        if not isinstance(text, str) or len(text.strip()) > limit or "\0" in text:
            raise ValueError(f"Book {key} must be text up to {limit} characters.")
        out[key] = text.strip()
    if not out["title"]:
        raise ValueError("Enter a book title.")
    if out["year"] and not re.fullmatch(r"[1-9][0-9]{3}", out["year"]):
        raise ValueError("Enter a four-digit publication year.")
    return out


def milliseconds(value):
    if type(value) is not int or not 0 <= value <= 10_000:
        raise ValueError("Use a whole number of milliseconds from 0 to 10,000.")
    return value


def _chapter_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120 or "\0" in value:
        raise ValueError("Each chapter needs a title of up to 120 characters.")
    return value.strip()


class Books:
    def __init__(self, scenes):
        self.scenes = scenes
        self.root = scenes.root / "out" / "books"
        self.lock = _path_lock(scenes.root / "data" / "scenes" / "books")
        with scenes._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS books (
                          id TEXT PRIMARY KEY, document TEXT NOT NULL, checksum TEXT NOT NULL, updated TEXT NOT NULL)""")

    def _read(self, db, book_id):
        book_id = self.scenes._validate_scene_id(book_id)
        row = db.execute("SELECT document,checksum FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise KeyError("Book project not found.")
        book = json.loads(row["document"])
        if book["id"] != book_id or digest_json(book) != row["checksum"]:
            raise RuntimeError("Book project checksum does not match.")
        return book

    def get(self, book_id):
        with self.scenes._connect() as db:
            return self._read(db, book_id)

    def summary(self, book_id):
        book = self.get(book_id)
        chapter_ids = book["chapters"]
        marks = ",".join("?" for _ in chapter_ids)
        with self.scenes._connect() as db:
            documents = {
                row["id"]: json.loads(row["manifest_json"])
                for row in db.execute(f"SELECT id,manifest_json FROM scenes WHERE id IN ({marks})", chapter_ids)
            }
            jobs = {}
            for row in db.execute(
                f"""SELECT id,scene_id,status FROM jobs
                                      WHERE scene_id IN ({marks}) ORDER BY created_at DESC,rowid DESC""",
                chapter_ids,
            ):
                jobs.setdefault(row["scene_id"], {"id": row["id"], "status": row["status"]})
        rows = []
        for scene_id in chapter_ids:
            scene = documents.get(scene_id)
            if not scene:
                raise KeyError("A chapter scene is missing from the library.")
            words = sum(len(MARKUP.findall(original_line(line))) for line in scene["lines"])
            parts = sum(len(line["segments"]) for line in scene["lines"])
            selected = sum(len(line["selected_takes"]) for line in scene["lines"])
            job = jobs.get(scene_id)
            status = (
                job["status"]
                if job and (job["status"] in ACTIVE or job["status"] in {"paused", "failed"})
                else (
                    "ready" if selected == parts else "review_takes" if job and job["status"] == "completed" else "needs_audio"
                )
            )
            rows.append(
                {
                    "id": scene_id,
                    "title": scene["name"],
                    "words": words,
                    "lines": len(scene["lines"]),
                    "segments": parts,
                    "selected": selected,
                    "status": status,
                    "job": job,
                    "estimated_seconds": round(
                        words * 60 / 150
                        + sum(line["pause_before_ms"] + line["pause_after_ms"] for line in scene["lines"]) / 1000
                    ),
                }
            )
        return {
            **book,
            "chapter_status": rows,
            "estimated_seconds": sum(row["estimated_seconds"] for row in rows) + (len(rows) - 1) * book["gap_ms"] / 1000,
        }

    def list(self):
        with self.scenes._connect() as db:
            rows = db.execute("SELECT document FROM books ORDER BY updated DESC,id").fetchall()
        return [
            {"id": book["id"], "title": book["metadata"]["title"], "chapters": len(book["chapters"])}
            for book in (json.loads(row["document"]) for row in rows)
        ]

    def chapter_books(self):
        with self.scenes._connect() as db:
            books = [json.loads(row[0]) for row in db.execute("SELECT document FROM books")]
        return {
            chapter: {"id": book["id"], "title": book["metadata"]["title"], "position": position}
            for book in books
            for position, chapter in enumerate(book["chapters"], 1)
        }

    def _chapter_scene(self, raw, roles, paragraph_pause, rules):
        if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list) or not 1 <= len(raw["lines"]) <= MAX_SCENE_LINES:
            raise ValueError("Each chapter needs 1–500 passages.")
        ignored, lines = [], []
        for index, row in enumerate(raw["lines"], 1):
            if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not isinstance(row.get("speaker"), str):
                raise ValueError("Each passage needs text and a speaker tag. Use an empty tag for the narrator.")
            source = row["text"].strip()
            if not source or len(source) > MAX_LINE_TEXT or "\0" in source:
                raise ValueError("Each passage needs up to 10,000 readable characters.")
            speaker = row["speaker"]
            if speaker not in roles:
                raise ValueError(f"Assign a Cast voice to {speaker or 'the narrator'} before creating this book.")
            voice = roles[speaker]
            direction = row.get("direction", "")
            if not isinstance(direction, str) or len(direction) > 240:
                raise ValueError("Shorten the acting direction to 240 characters and try again.")
            if direction and voice["engine_id"] not in DIRECTION_ENGINES:
                ignored.append(index)
            after = row.get("pause_after_ms")
            spoken = spoken_line(source, rules)
            options = voice["engine_options"]
            language = row.get("language") or options.get("language")
            if not language:
                adapter = engine_adapters.get(voice["engine_id"])
                languages = adapter.voice_languages(options) if adapter else None
                language = languages[0] if languages else None
            lines.append(
                {
                    "line_id": f"line-{index:04d}",
                    "line_type": "dialogue" if speaker else "narration",
                    "identity_id": voice["id"],
                    "character_id": voice["character_id"],
                    "text": spoken,
                    "manuscript_text": source,
                    "manuscript_spoken": spoken,
                    "manuscript_speaker": speaker,
                    "manuscript_direction": direction,
                    "direction": direction if voice["engine_id"] in DIRECTION_ENGINES else "",
                    **({"language": language} if language else {}),
                    "settings": row.get("settings") or {},
                    "pause_before_ms": milliseconds(row.get("pause_before_ms", 0)),
                    "pause_after_ms": paragraph_pause if after is None else milliseconds(after),
                    "take_count": 1,
                }
            )
        return {"name": _chapter_name(raw.get("title")), "lines": lines}, ignored

    def create(self, request):
        raw = request.get("chapters")
        voices = request.get("voices")
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CHAPTERS or not isinstance(voices, dict) or len(voices) > 41:
            raise ValueError("Preview the manuscript and choose voices for up to 160 chapters.")
        book_id, stamp = new_id(), utc_now()
        info, roles = metadata(request.get("metadata")), {}
        para = milliseconds(request.get("paragraph_pause_ms", 275))
        rules = pronunciations(request.get("pronunciations", {}))
        for speaker, identity in voices.items():
            if not isinstance(speaker, str) or len(speaker) > 60:
                raise ValueError("Each voice mapping needs a speaker name up to 60 characters.")
            voice = self.scenes.cast.get_generation_identity(identity)
            if voice["status"] != "approved":
                raise ValueError(f"Approve the Cast voice for {speaker or 'the narrator'} before creating the book.")
            roles[speaker] = voice
        source = request.get("source", {})
        if not isinstance(source, dict) or set(source) - {"filename", "sha256"}:
            raise ValueError("The book source can record a file name and its SHA-256.")
        if source and (
            not isinstance(source.get("filename"), str)
            or len(source["filename"]) > 240
            or not isinstance(source.get("sha256"), str)
            or not HEX.fullmatch(source["sha256"])
        ):
            raise ValueError("Invalid manuscript preview source identity.")
        normalized, warnings = [], []
        for entry in raw:
            payload, ignored = self._chapter_scene(entry, roles, para, rules)
            if ignored:
                warnings.append(
                    f"{payload['name']}: the chosen models use no acting direction on {len(ignored)} passage(s). Choose a model that accepts it to use those directions."
                )
            normalized.append(payload)
        lines = [line for part in normalized for line in part["lines"]]
        if len(lines) > MAX_LINES or sum(len(line["manuscript_text"]) for line in lines) > MAX_TEXT:
            raise ValueError("Use at most 6,000 passages and 700,000 manuscript characters per book.")
        scenes = [
            self.scenes._normalize_scene({**part, "book_id": book_id, "book_pronunciations": rules}, new_id(), 1)
            for part in normalized
        ]
        book = {
            "schema_version": 1,
            "id": book_id,
            "revision": 1,
            "metadata": info,
            "chapters": [scene["scene_id"] for scene in scenes],
            "source": source,
            "created": stamp,
            "gap_ms": milliseconds(request.get("gap_ms", 1_000)),
            "pronunciations": rules,
            "cover": None,
        }
        with self.lock, self.scenes._connect() as db:
            db.execute("INSERT INTO books VALUES(?,?,?,?)", (book_id, canonical_json(book), digest_json(book), stamp))
            for scene in scenes:
                self.scenes._insert_scene(db, scene, stamp)
        for scene in scenes:
            self.scenes._write_scene_file(scene)
        return self.summary(book_id), warnings

    def _store(self, db, book, old):
        if type(old) is not int or old != book["revision"]:
            raise ValueError("This book changed. Reopen it before saving.")
        book["revision"] += 1
        db.execute(
            "UPDATE books SET document=?,checksum=?,updated=? WHERE id=?",
            (canonical_json(book), digest_json(book), utc_now(), book["id"]),
        )

    def update(self, book_id, request):
        changed = []
        with self.lock, self.scenes._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            book = self._read(db, book_id)
            if book["revision"] != request.get("expected_revision"):
                raise ValueError("This book changed. Reopen it before saving.")
            if "metadata" in request:
                book["metadata"] = metadata(request["metadata"])
            if "gap_ms" in request:
                book["gap_ms"] = milliseconds(request["gap_ms"])
            if "chapters" in request:
                order = request["chapters"]
                if (
                    not isinstance(order, list)
                    or any(not isinstance(key, str) for key in order)
                    or sorted(order) != sorted(book["chapters"])
                ):
                    raise ValueError("Reorder the book's existing chapters.")
                book["chapters"] = order
            new_rules = pronunciations(request["pronunciations"]) if "pronunciations" in request else book["pronunciations"]
            if new_rules != book["pronunciations"]:
                for scene_id in book["chapters"]:
                    scene = self.scenes.get_scene(scene_id)
                    if any(job["status"] in ACTIVE for job in self.scenes.list_jobs(scene_id)):
                        raise ValueError("Pause running chapter jobs or let them finish before changing pronunciations.")
                    payload = {**scene, "lines": [dict(line) for line in scene["lines"]]}
                    for line in payload["lines"]:
                        original = original_line(line)
                        spoken = spoken_line(original, new_rules)
                        line.update(text=spoken, manuscript_text=original, manuscript_spoken=spoken)
                    payload["book_pronunciations"] = new_rules
                    if payload["lines"] != scene["lines"] or new_rules != scene.get("book_pronunciations", []):
                        item = self.scenes._normalize_scene(payload, scene_id, scene["revision"] + 1, previous=scene)
                        self.scenes._update_scene(db, item, utc_now(), scene["revision"])
                        changed.append(item)
                book["pronunciations"] = new_rules
            self._store(db, book, request.get("expected_revision"))
        for item in changed:
            self.scenes._write_scene_file(item)
        return self.summary(book_id)

    def set_cover(self, book_id, revision, asset):
        with self.lock, self.scenes._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            book = self._read(db, book_id)
            book["cover"] = asset
            self._store(db, book, revision)
        return self.summary(book_id)

    def chapter(self, book_id, scene_id):
        book = self.get(book_id)
        if scene_id not in book["chapters"]:
            raise KeyError("Chapter does not belong to this book.")
        return self.scenes.get_scene(scene_id)

    def queue(self, book_id, chapters=None, rerender=False):
        book = self.get(book_id)
        if chapters is None:
            chapters = book["chapters"]
        if (
            type(rerender) is not bool
            or not isinstance(chapters, list)
            or not chapters
            or any(not isinstance(key, str) or key not in book["chapters"] for key in chapters)
            or len(set(chapters)) != len(chapters)
        ):
            raise ValueError("Choose one or more distinct chapters in this book.")
        work, jobs = [], []
        with self.lock:
            for key in chapters:
                recent = self.scenes.list_jobs(key)
                job = recent[0] if recent else None
                if job and job["status"] in ACTIVE:
                    jobs.append(job)
                    continue
                if job and job["status"] == "paused":
                    if self.scenes.get_scene(key)["checksum"] != job["scene_checksum"]:
                        raise ValueError("This paused chapter changed. Cancel its old job before rendering the new edit.")
                    work.append(("resume", job["id"], None))
                    continue
                states = self.scenes.line_states(key)
                candidates = [
                    line["line_id"]
                    for line in states["lines"]
                    if any(segment["status"] == "choose_take" for segment in line["segments"])
                ]
                if candidates and not rerender:
                    self.scenes.choose_newest_takes(key, candidates)
                    states = self.scenes.line_states(key)
                segments = [
                    line["line_id"] + "/" + row["segment_id"]
                    for line in states["lines"]
                    for row in line["segments"]
                    if rerender or row["status"] != "done"
                ]
                if segments:
                    work.append(("render", key, segments))
            for action, key, segments in work:
                jobs.append(
                    self.scenes.resume(key) if action == "resume" else self.scenes.create_job(key, segment_ids=segments)
                )
        return jobs

    def choose(self, book_id, scene_id):
        self.chapter(book_id, scene_id)
        scene, chosen, _states = self.scenes.choose_newest_takes(scene_id)
        return {"chapter": scene, "chosen": chosen}
