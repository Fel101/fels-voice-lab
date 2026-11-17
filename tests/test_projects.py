"""Portable projects and voice files round-trip through the local Lab."""

import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile

import numpy as np
import soundfile as sf

from cast_store import CastStore
from scene_store import SceneStore
from tests.test_export_panel import ExportPanelBase
import portable_projects
from tests.test_browser_flow import BROWSER, DevToolsPage


class ProjectFilesTests(ExportPanelBase):
    def request(self, path, data=None, metadata=None):
        headers = {}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif isinstance(data, bytes):
            headers["Content-Type"] = "application/octet-stream"
        if metadata is not None:
            headers["X-Reference-Metadata"] = base64.urlsafe_b64encode(json.dumps(metadata).encode()).decode()
        request = urllib.request.Request(self.base + path, data, headers)
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def checked_json(self, path, status, data=None, metadata=None):
        actual, raw = self.request(path, data, metadata)
        result = json.loads(raw)
        self.assertEqual(actual, status, result)
        return result

    def test_voice_file_checks_permissions_and_keeps_audition_and_cast_details(self):
        self.render_selected_scene()
        self.cast.save_pronunciation(source="harbor", replacement="har bor", character_id=self.character["id"])
        request = {"kind": "persona", "identity_id": self.identity["id"], "author": "Test fixture"}
        self.assertIn("permission", self.checked_json("/api/portable/export", 400, request)["error"])
        result = self.checked_json("/api/portable/export", 201, {**request, "permission": True})
        status, raw = self.request(result["archive"]["download"])
        self.assertEqual(status, 200)
        before = self.cast.list_cast()
        preview = self.checked_json("/api/portable/preview", 200, raw)["archive"]
        self.assertEqual(self.cast.list_cast(), before)
        self.assertEqual(preview["author"], "Test fixture")
        item = preview["voices"][0]
        self.assertEqual(item["sample"]["origin"], "local test fixture")
        self.assertEqual(item["sample"]["allowed_use"], "tests only")
        status, audition = self.request("/api/portable/voice-preview?asset=" + item["audition"]["asset"], raw)
        self.assertEqual(status, 200)
        with tempfile.TemporaryDirectory() as temporary:
            target = SceneStore(CastStore(Path(temporary)), self.engines.entries)
            options = {"archive_sha256": preview["archive_sha256"]}
            with self.assertRaisesRegex(ValueError, "allowed use"):
                portable_projects.import_archive(target, raw, options)
            self.assertEqual(target.cast.list_cast(), [])
            result = portable_projects.import_archive(target, raw, {**options, "permission": True})
            identity = result["identity"]
            self.assertEqual(identity["status"], "candidate")
            self.assertIsNone(identity["approved_reference_id"])
            self.assertNotEqual(identity["id"], self.identity["id"])
            character = target.cast.get_character(identity["character_id"])
            self.assertEqual(character["name"], self.character["name"])
            self.assertEqual(character["description"], self.character["description"])
            self.assertEqual(target.cast.list_pronunciations(character["id"])[0]["replacement"], "har bor")
            [take] = target.cast.list_takes(identity["id"])
            restored, rate = sf.read(target.cast.get_media_path("take", take["id"]), dtype="int16")
            original, source_rate = sf.read(io.BytesIO(audition), dtype="int16")
            self.assertEqual(rate, source_rate)
            np.testing.assert_array_equal(restored, original)

        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(broken, "w") as archive:
            for name in source.namelist():
                data = source.read(name)
                if name == item["audition"]["asset"]:
                    data = data[:-1] + bytes([data[-1] ^ 1])
                archive.writestr(name, data)
        self.assertIn("SHA-256", self.checked_json("/api/portable/preview", 400, broken.getvalue())["error"])

    @unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
    def test_page_groups_subtitle_sections_and_book_chapters_and_reports_line_counts(self):
        self.start_server()
        book = self.checked_json(
            "/api/books",
            201,
            {
                "metadata": {"title": "Harbor book"},
                "chapters": [
                    {"title": name, "lines": [{"text": "One passage.", "speaker": ""}]} for name in ("Opening", "Closing")
                ],
                "voices": {"": self.identity["id"]},
            },
        )["book"]
        subtitles = "\n\n".join(f"{i + 1}\n0{i}:31:00,000 --> 0{i}:31:01,000\nMira: A timed line." for i in range(3))
        scenes, _ = self.scenes.import_script(
            subtitles, "srt", {"Mira": {"identity_id": self.identity["id"]}}, name="Harbor film"
        )
        ids = [scene["scene_id"] for scene in scenes]
        self.assertEqual(len(ids), 3)
        for scene_id in (ids[2], book["chapters"][1]):
            scene = self.scenes.get_scene(scene_id)
            if scene_id == ids[2]:
                scene["name"] = "Harbor film finale"
            self.scenes.update_scene(scene_id, scene, expected_revision=scene["revision"])
        regular = self.scene_payload("Harbor scene")
        regular["lines"] = regular["lines"][:1]
        self.scenes.create_scene(regular)

        page = DevToolsPage(BROWSER)
        self.addCleanup(page.close)
        page.open(self.base + "/#scenes")
        page.wait_until("$('sceneSelect').options.length === 6")
        choices = page.evaluate("Array.from($('sceneSelect').options, o => o.value)")
        self.assertEqual([key for key in choices if key in ids], ids)
        self.assertEqual([key for key in choices if key in book["chapters"]], book["chapters"])
        page.show("projects")
        page.wait_until("$('projectRows').children.length === 5")
        rows = page.evaluate("Array.from($('projectRows').children, row => row.textContent)")
        self.assertTrue(all("1 line · 1 voice" in row for row in rows if "Harbor book" not in row), rows)
        self.assertIn("2 passages · 1 voice", next(row for row in rows if "Harbor book" in row))
        page.show("cast")
        page.wait_until("document.querySelector('[aria-label=\"Export this voice: Mira\"]') !== null")
        self.assertEqual(page.problems, [])

    @unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
    def test_kokoro_voice_file_preview_uses_the_model_card_words(self):
        self.start_server()
        voice = self.cast.create_identity(self.character["id"], "kokoro", "Harbor preset", {"voice": "af_heart"})
        self.cast.update_identity(voice["id"], {"status": "approved"})
        generation = self.cast.get_generation_identity(voice["id"])
        self.cast.create_take(
            generation,
            line_id="proof",
            text="Listen to the harbor.",
            spoken_text="Listen to the harbor.",
            language="American English",
            direction="",
            settings={},
            model={"backend": "fixture"},
            wav=np.sin(np.arange(2400) * 0.08).astype(np.float32) * 0.1,
            sample_rate=24000,
            generation_info={},
        )
        archive = self.checked_json("/api/portable/export", 201, {"kind": "persona", "identity_id": voice["id"]})["archive"]
        status, raw = self.request(archive["download"])
        self.assertEqual(status, 200)
        file = self.root / "harbor.voice.zip"
        file.write_bytes(raw)
        models = self.checked_json("/api/catalog", 200)["engines"]
        words = next(entry["facts"]["output"]["text"] for entry in models if entry["id"] == "kokoro")
        self.assertEqual(words, "What its audio may be used for: not stated.")
        page = DevToolsPage(BROWSER)
        self.addCleanup(page.close)
        page.open(self.base + "/#projects")
        page.click("#portableImportPanel summary")
        page.set_files("#portableFile", file)
        page.click("#previewPortable")
        page.wait_until("!$('portableReview').hidden")
        self.assertIn(words, page.evaluate("$('portableMappings').textContent"))
        self.assertEqual(page.problems, [])

    def test_projects_find_and_copy_a_scene_with_local_voice_assignments(self):
        scene_id = self.render_selected_scene()
        report = self.checked_json("/api/projects?q=road", 200)["projects"]
        self.assertEqual([row["id"] for row in report], [scene_id])
        item = self.checked_json("/api/portable/export", 201, {"kind": "scene", "id": scene_id})["archive"]
        status, raw = self.request(item["download"])
        self.assertEqual(status, 200)
        report = self.checked_json("/api/portable/preview", 200, raw)["archive"]
        copy = self.checked_json(
            "/api/portable/import",
            201,
            raw,
            {"archive_sha256": report["archive_sha256"], "voices": {self.identity["id"]: self.identity["id"]}},
        )["imported"]
        self.assertNotEqual(copy["project_id"], scene_id)
        new = self.scenes.get_scene(copy["project_id"])
        self.assertEqual(new["imported_from"]["scene_id"], scene_id)
        self.assertEqual(new["lines"][0]["text"], "The road is clear.")
        self.assertFalse(new["lines"][0]["selected_takes"])
