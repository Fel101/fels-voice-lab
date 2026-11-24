"""Audiobook manuscript preview, editing, render and delivery routes."""

from functools import cache
from urllib.parse import parse_qs

from voice_lab import book_export
from voice_lab.book_store import Books
from voice_lab import manuscript_import


def register(router, lab):
    @cache
    def books():
        return Books(lab.scenes)

    @router.get("/api/books")
    def book_list(self, parsed, req):
        self._json(200, {"books": books().list()})

    @router.post("/api/books/import/preview")
    def book_preview(self, parsed, req):
        query = parse_qs(parsed.query)
        name = query.get("name", [""])[0]
        split = query.get("split", ["paragraph"])[0]
        preview = manuscript_import.preview(self._read_body(manuscript_import.MAX_UPLOAD), name, split)
        checks = getattr(lab.engines, "check_store", None)
        speed = {}
        if checks:
            # Prefer the requested device but keep the latest CPU measurement when the model can only use the CPU.
            rows = sorted(
                checks.speed_rows(),
                key=lambda row: (row["device"] == lab.engines.device_for(row["engine"]), row["last_measured_at"]),
            )
            speed = {row["engine"]: row["median_real_time_factor"] for row in rows}
        self._json(200, {"manuscript": preview, "model_speed": speed})

    @router.post("/api/books")
    def create_book(self, parsed, req):
        book, warnings = books().create(req)
        self._json(201, {"book": book, "warnings": warnings})

    @router.get("/api/books/{book_id}")
    def read_book(self, parsed, req, book_id):
        self._json(200, {"book": books().summary(book_id), "delivery": book_export.latest(books(), book_id)})

    @router.post("/api/books/{book_id}")
    def save_book(self, parsed, req, book_id):
        self._json(200, {"book": books().update(book_id, req)})

    @router.post("/api/books/{book_id}/cover")
    def attach_cover(self, parsed, req, book_id):
        try:
            stamp = int(parse_qs(parsed.query).get("revision", [""])[0])
        except ValueError as exc:
            raise ValueError("Supply the saved book revision before attaching cover art.") from exc
        self._json(201, {"book": book_export.attach_cover(books(), book_id, stamp, self._read_body(book_export.COVER_LIMIT))})

    @router.get("/api/books/{book_id}/cover.jpg")
    def read_cover(self, parsed, req, book_id):
        book = books().get(book_id)
        cover = book["cover"]
        if not cover:
            raise KeyError("Book cover not found.")
        path = books().root / book_id / "covers" / (cover["sha256"] + ".jpg")
        if not path.is_file() or book_export.sha256_file(path) != cover["sha256"]:
            raise ValueError("The saved cover did not pass its checksum.")
        self._send_path(path, "image/jpeg")

    @router.post("/api/books/{book_id}/queue")
    def queue_chapters(self, parsed, req, book_id):
        jobs = books().queue(book_id, req.get("chapters"), req.get("rerender", False))
        if jobs:
            lab.batches.wake()
        self._json(
            202,
            {
                "jobs": [{"id": job["id"], "scene_id": job["scene_id"], "status": job["status"]} for job in jobs],
                "book": books().summary(book_id),
            },
        )

    @router.post("/api/books/{book_id}/chapters/{scene_id}/choose")
    def choose_takes(self, parsed, req, book_id, scene_id):
        self._json(200, books().choose(book_id, scene_id))

    @router.post("/api/books/{book_id}/chapters/{scene_id}/audition")
    def preview_chapter(self, parsed, req, book_id, scene_id):
        preview, _ = book_export.audition(books(), book_id, scene_id)
        self._json(201, {"audition": preview, "audio_url": f"/api/books/{book_id}/auditions/{preview['id']}/chapter.wav"})

    @router.post("/api/books/{book_id}/exports")
    def publish_book(self, parsed, req, book_id):
        self._json(201, {"delivery": book_export.export(books(), book_id, req)})

    @router.post("/api/books/{book_id}/exports/{delivery_id}/verify")
    def verify_book(self, parsed, req, book_id, delivery_id):
        self._json(200, book_export.verify(books(), book_id, delivery_id))

    @router.get(
        "/api/books/{book_id}/exports/{delivery_id}/{book_file}", "/api/books/{book_id}/auditions/{delivery_id}/{book_file}"
    )
    def book_file_bytes(self, parsed, req, book_id, delivery_id, book_file):
        kind = "auditions" if "/auditions/" in parsed.path else "deliveries"
        path = book_export.path_for(books(), book_id, kind, delivery_id, book_file)
        self._send_path(path, book_export.MEDIA[path.suffix.lstrip(".")])
