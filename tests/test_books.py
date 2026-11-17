"""Milestone 4 manuscript formats and book workflow acceptance."""

import json
import struct
import time
import unittest
import urllib.error
import urllib.request
import zlib

import soundfile as sf

from book_store import Books
import manuscript_import
from scene_store import SceneBatchRunner, SceneStore
from tests.test_export_panel import HAS_FFMPEG, ExportPanelBase, tone_wav

TEXT = """# A Light at the Harbor
The tide rose. The lamp burned.

[voice:Mira] Keep watch. [pause 0.5s]

# A Voice in the Fog
[voice:Tobin] Call out now.

The echo came back.

# The Crossing
[voice:Mira] I can see the shore.

[voice:Tobin] Steady as we go.
"""


def picture():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)
    rgb = b"".join(b"\0" + bytes((12, 63, 98)) * 16 for _ in range(16))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rgb)) + chunk(b"IEND", b"")


class ImportTests(unittest.TestCase):
    def test_text_splits_and_preserves_authored_voice_and_pause(self):
        rows = manuscript_import.preview(TEXT.encode("utf-16"), "Tide.md")
        self.assertEqual(
            [part["title"] for part in rows["chapters"]], ["A Light at the Harbor", "A Voice in the Fog", "The Crossing"]
        )
        self.assertEqual(rows["speakers"], ["Mira", "Tobin"])
        self.assertEqual(rows["chapters"][0]["lines"][1]["pause_after_ms"], 500)
        self.assertTrue(rows["narration"])
        sentence = manuscript_import.preview(TEXT.encode(), "Tide.md", "sentence")
        self.assertEqual(
            [line["text"] for line in sentence["chapters"][0]["lines"][:2]], ["The tide rose.", "The lamp burned."]
        )
        whole = manuscript_import.preview(b"One paragraph.\n\nAnother paragraph.", "Tide.txt", "chapter")
        self.assertEqual(len(whole["chapters"][0]["lines"]), 1)

    def test_prose_is_never_taken_for_a_numbered_heading(self):
        speech = [
            "The silver ferry came slowly out of the fog.",
            "Part of the crew stayed below, out of the cold.",
            "Book a berth before sunrise.",
            "Chapter and verse were read aloud.",
        ]
        for format_, prefix, suffix in (("txt", "Chapter II", "Chapter 3"), ("md", "# Across the water", "# Safe on shore")):
            for separator in ("\n", "\n\n"):
                with self.subTest(format=format_, hard_wrapped=separator == "\n"):
                    source = prefix + "\n\n" + separator.join(speech) + "\n\n" + suffix
                    preview = manuscript_import.preview(source.encode(), f"Harbor.{format_}")
                    titles = " ".join(chapter["title"] for chapter in preview["chapters"])
                    words = " ".join(row["text"] for chapter in preview["chapters"] for row in chapter["lines"])
                    self.assertIn(prefix.lstrip("# "), titles)
                    self.assertIn(suffix.lstrip("# "), titles)
                    self.assertEqual(words, " ".join(speech))

    def test_standalone_numbered_and_consecutive_headings_stay_in_the_preview(self):
        for format_, source, title in (
            ("md", "# Part One\n## Chapter 1\nThe voyage began.", "Part One: Chapter 1"),
            ("txt", "Chapter 1 The Harbor\n\nThe voyage began.", "Chapter 1 The Harbor"),
            ("txt", "Book Ninety-Nine: The Port\n\nChapter 3\n\nThe voyage began.", "Book Ninety-Nine: The Port: Chapter 3"),
        ):
            with self.subTest(format=format_):
                preview = manuscript_import.preview(source.encode(), f"Harbor.{format_}")
                self.assertEqual(preview["chapters"][0]["title"], title)
                self.assertEqual(preview["chapters"][0]["lines"][0]["text"], "The voyage began.")
        prose = b"The ferry moved.\nPart II\nThe sea was rough."
        preview = manuscript_import.preview(prose, "Harbor.txt")
        self.assertEqual(preview["chapters"][0]["lines"][0]["text"], "The ferry moved. Part II The sea was rough.")

    def test_split_chapters_explain_the_numbered_sections_and_export_marks(self):
        content = "# Harbor notes\n\n" + "\n\n".join(f"Passage {index}." for index in range(501))
        preview = manuscript_import.preview(content.encode(), "Harbor.md")
        self.assertEqual([row["title"] for row in preview["chapters"]], ["Harbor notes (1 of 2)", "Harbor notes (2 of 2)"])
        self.assertIn("chapter mark on export", preview["warnings"][0])

    def test_a_two_thousand_passage_manuscript_keeps_chapters_separate(self):
        text = "\n\n".join(
            f"# Part {index}\n\n" + "\n\n".join(f"Passage {line}. The tide is high." for line in range(100))
            for index in range(20)
        )
        result = manuscript_import.preview(text.encode(), "Volume.md")
        self.assertEqual(len(result["chapters"]), 20)
        self.assertEqual(sum(len(part["lines"]) for part in result["chapters"]), 2000)


class BookTests(ExportPanelBase):
    def start_server(self):
        self.engines.engine_status = lambda: {"device": "cpu", "engines": [], "speed": []}
        return super().start_server()

    def request(self, path, body=None, headers=None):
        if isinstance(body, dict):
            headers = {"Content-Type": "application/json", **(headers or {})}
            body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=50) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def checked_json(self, path, status, body=None):
        got, _, raw = self.request(path, body)
        payload = json.loads(raw)
        self.assertEqual(got, status, payload)
        return payload

    def new_book(self):
        character = self.cast.create_character("Tobin", "Second voice")
        identity = self.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", "Tobin")
        ref = self.cast.add_reference(
            identity["id"],
            "tobin.wav",
            tone_wav(),
            transcript="Synthetic audition.",
            origin="fixture",
            allowed_use="local test",
        )
        self.cast.update_identity(identity["id"], {"status": "approved", "reference_version_id": ref["id"]})
        draft = self.checked_json("/api/books/import/preview?name=Harbor.md", 200, TEXT.encode())["manuscript"]
        self.checked_json(
            "/api/books",
            400,
            {
                "metadata": {"title": "Tide"},
                "chapters": draft["chapters"],
                "voices": {"": self.identity["id"], "Mira": self.identity["id"]},
            },
        )
        self.assertFalse(self.scenes.list_scenes(), "An unmapped project must leave no stray chapter scenes.")
        return self.checked_json(
            "/api/books",
            201,
            {
                "metadata": {
                    "title": "The Harbor #1; A Book",
                    "author": "T. Fixture",
                    "narrator": "Mira and Tobin",
                    "description": "Say = listen\nA second line.",
                },
                "chapters": draft["chapters"],
                "source": {"filename": draft["filename"], "sha256": draft["sha256"]},
                "voices": {"": self.identity["id"], "Mira": self.identity["id"], "Tobin": identity["id"]},
                "pronunciations": {"harbor": "har-bor"},
                "gap_ms": 650,
            },
        )["book"]

    def wait_render(self, jobs):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            values = [self.scenes.get_job(job["id"]) for job in jobs]
            if all(job["status"] == "completed" for job in values):
                return values
            self.assertFalse(any(value["status"] == "failed" for value in values), values)
            time.sleep(0.025)
        self.fail(f"Book jobs did not finish: {values}")

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg is needed for audiobook package checks")
    def test_book_recovers_after_interruption_and_only_edited_chapter_rerenders(self):
        self.start_server()
        book = self.new_book()
        id_, ids = book["id"], book["chapters"]
        url = f"/api/books/{id_}"
        book = self.checked_json(url + "/cover?revision=1", 201, picture())["book"]
        self.assertTrue(book["cover"]["sha256"])
        jobs = self.checked_json(url + "/queue", 202, {})["jobs"]
        first = jobs[0]["id"]
        self.scenes.start_queued_job(first)
        row = self.scenes.get_job_row(self.scenes.claim_next_row(first))
        call = {
            key: row[key]
            for key in (
                "identity_id",
                "reference_version_id",
                "text",
                "language",
                "direction",
                "settings",
                "use_pronunciations",
            )
        }
        status, response, raw = self.request(
            "/api/performance", {**call, "line_id": row["cast_line_id"], "idempotency_key": row["request_key"]}
        )
        self.assertEqual(status, 200, raw[:100])
        saved = response["X-Take-Id"]
        self.scenes = SceneStore(self.cast, self.engines.entries, root=self.root)
        self.runner = SceneBatchRunner(self.scenes, self.base)
        self.runner.start()
        complete = self.wait_render(jobs)
        replay = next(item for item in complete if item["id"] == first)
        self.assertIn(saved, [part["take_id"] for part in replay["rows"]])
        for scene_id in ids:
            self.checked_json(f"{url}/chapters/{scene_id}/choose", 200, {})
        preview = self.checked_json(f"{url}/chapters/{ids[0]}/audition", 201, {})
        self.assertEqual(self.request(preview["audio_url"], headers={"Range": "bytes=0-19"})[0], 206)
        delivery = self.checked_json(url + "/exports", 201, {"expected_revision": book["revision"]})["delivery"]
        self.assertEqual(delivery["chapters"][0]["cache_id"], preview["audition"]["id"])
        self.assertTrue(delivery["m4b"]["attached_cover"])
        self.assertIn("chapter-003.flac", delivery["files"])
        self.assertIn("book.cue", delivery["files"])
        self.assertTrue(delivery["loudness"]["true_peak_dbtp"] < 0)
        first_paths = [self.scenes.get_scene(scene_id)["lines"][0]["selected_take_id"] for scene_id in ids]
        chapter = self.scenes.get_scene(ids[1])
        chapter["lines"][0]["text"] = "Look toward the harbor."
        revised = self.scenes.update_scene(ids[1], chapter, chapter["revision"])
        self.assertEqual(revised["lines"][0]["text"], "Look toward the har-bor.")
        self.assertEqual(revised["lines"][0]["manuscript_text"], "Look toward the harbor.")
        jobs = self.checked_json(url + "/queue", 202, {})["jobs"]
        self.assertEqual([job["scene_id"] for job in jobs], [ids[1]])
        result = self.wait_render(jobs)[0]
        self.assertEqual(len(result["rows"]), 1, result)
        self.checked_json(f"{url}/chapters/{ids[1]}/choose", 200, {})
        second = self.checked_json(url + "/exports", 201, {"expected_revision": book["revision"]})["delivery"]
        self.assertEqual(
            [self.scenes.get_scene(scene_id)["lines"][0]["selected_take_id"] for scene_id in ids][::2], first_paths[::2]
        )
        self.assertEqual(
            [part["cache_id"] for part in second["chapters"]][::2], [part["cache_id"] for part in delivery["chapters"]][::2]
        )
        self.assertNotEqual(second["chapters"][1]["cache_id"], delivery["chapters"][1]["cache_id"])
        base = url + "/exports/" + second["id"]
        self.assertTrue(self.checked_json(base + "/verify", 200, {})["ok"])
        self.assertIn("Look toward the harbor.", self.request(base + "/book.srt")[2].decode())
        path = Books(self.scenes).root / id_ / "deliveries" / second["id"] / "book.wav"
        self.assertEqual(sf.info(path).frames, second["chapters"][-1]["end_frame"])
        self.assertEqual(self.request(base + "/book.m4b", headers={"Range": "bytes=999999999-"})[0], 416)
        path.write_bytes(b"broken")
        self.assertIn("SHA-256", self.checked_json(base + "/verify", 400, {})["error"])

    def test_large_book_creates_editable_chapter_scenes(self):
        self.start_server()
        draft = manuscript_import.preview(
            "\n\n".join(
                f"# Part {index}\n\n" + "\n\n".join(f"Passage {line}. The tide is high." for line in range(100))
                for index in range(20)
            ).encode(),
            "Large book.md",
        )
        book = self.checked_json(
            "/api/books",
            201,
            {"metadata": {"title": "Large book"}, "chapters": draft["chapters"], "voices": {"": self.identity["id"]}},
        )["book"]
        self.assertEqual(sum(chapter["lines"] for chapter in book["chapter_status"]), 2_000)
        chapter_id = book["chapters"][10]
        chapter = self.scenes.get_scene(chapter_id)
        self.assertEqual(len(chapter["lines"]), 100)
        chapter["lines"][-1]["text"] = "An edited final passage."
        revised = self.checked_json("/api/scenes/" + chapter_id, 200, {**chapter, "expected_revision": chapter["revision"]})
        self.assertEqual(revised["scene"]["lines"][-1]["manuscript_text"], "An edited final passage.")

    @unittest.skipUnless(HAS_FFMPEG, "Needs FFmpeg for book export")
    def test_book_browser_preview_create_edit_audition_and_export(self):
        from tests.test_browser_flow import BROWSER, DevToolsPage

        if not BROWSER:
            self.skipTest("No headless Chromium browser installed.")
        self.start_server()
        self.runner = self.server.RequestHandlerClass.lab.batches
        self.runner.start()
        page = DevToolsPage(BROWSER)
        try:
            page.open(self.base + "/#books")
            page.set_value("#bookPaste", "# Harbor lights\n\nThere is a light ahead.")
            page.click("#bookPreviewPaste")
            page.wait_until("!document.getElementById('bookReview').hidden")
            page.set_value("#bookVoiceMap select", self.identity["id"])
            page.set_value("#bookImportTitle", "Harbor lights")
            page.click("#bookCreate")
            page.wait_until("document.querySelectorAll('#bookChapters tr').length === 1")
            self.assertIn("cannot be stopped", page.evaluate("document.getElementById('bookExportTiming').textContent"))
            page.evaluate("document.getElementById('bookPronounce').closest('details').open = true")
            page.set_value("#bookPronounce", "harbor = har-bor")
            page.click("#bookSave")
            page.wait_until("document.getElementById('bookStatus').textContent.includes('Book details saved')")
            page.click('#bookChapters button[aria-label^="Edit chapter"]')
            page.wait_until("location.hash === '#scenes' && Boolean(activeSceneId)")
            page.wait_until("document.querySelector('.scene-row') !== null")
            self.assertIn(
                "Harbor lights", page.evaluate("document.getElementById('sceneSelect').selectedOptions[0].textContent")
            )
            page.evaluate("location.hash = '#books'")
            page.wait_until("!document.getElementById('bookProject').hidden")
            page.click("#bookQueue")
            page.wait_until("document.getElementById('bookChapters').textContent.includes('review takes')")
            page.click('#bookChapters button[aria-label^="Use newest takes for "]')
            page.wait_until("document.getElementById('bookChapters').textContent.includes('ready')")
            page.click('#bookChapters button[aria-label^="Audition"]')
            page.wait_until("document.getElementById('bookAudition').src.endsWith('/chapter.wav')")
            cover = self.root / "book.png"
            cover.write_bytes(picture())
            page.evaluate("document.getElementById('bookCover').closest('details').open = true")
            page.set_files("#bookCover", cover)
            page.click("#bookUploadCover")
            page.wait_until("document.getElementById('bookCoverPreview').naturalWidth > 0")
            page.click("#bookExport")
            page.wait_until("document.querySelector('#bookDownloads a[download=\"book.m4b\"]') !== null", timeout=30)
            page.click("#bookVerify")
            page.wait_until("document.getElementById('bookStatus').textContent.includes('chapter verified')")
            self.assertEqual(page.problems, [])
        finally:
            page.close()
