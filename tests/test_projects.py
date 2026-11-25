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
from unittest import mock

import numpy as np
import soundfile as sf

from voice_lab.cast_store import CastStore
from voice_lab.scene_store import SceneStore
from tests.test_export_panel import HAS_FFMPEG, ExportPanelBase, tone_wav
from voice_lab import portable_projects
from voice_lab import connector_profiles
from voice_lab import dubbing
from app_version import REPOSITORY
from tests.test_browser_flow import BROWSER, DevToolsPage


class ProjectFilesTests(ExportPanelBase):
    def open_page(self):
        # Closed in tearDown, before the base class removes the library: a page still loading audio holds its file open.
        page = DevToolsPage(BROWSER)
        self.pages = [*getattr(self, "pages", []), page]
        return page

    def tearDown(self):
        for page in getattr(self, "pages", []):
            page.close()
        super().tearDown()

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
        self.cast.update_identity(self.identity["id"], {"licence_rating": "no"})
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
        self.assertEqual(item["licence"]["rating"], "no")
        self.assertEqual(item["audition"]["licence"], item["licence"])
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
            self.assertEqual(identity["licence"]["rating"], "unclear")
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

    def test_projects_list_subtitle_sections_in_their_own_order_after_an_edit(self):
        # Projects and the recipe scene picker read this list, as Scenes reads its own.
        self.start_server()
        subtitles = "\n\n".join(f"{i + 1}\n0{i}:31:00,000 --> 0{i}:31:01,000\nMira: A timed line." for i in range(3))
        scenes, _ = self.scenes.import_script(
            subtitles, "srt", {"Mira": {"identity_id": self.identity["id"]}}, name="Harbor film"
        )
        ids = [scene["scene_id"] for scene in scenes]
        last = self.scenes.get_scene(ids[2])
        last["name"] = "Harbor film finale"
        self.scenes.update_scene(ids[2], last, expected_revision=last["revision"])
        listed = [row["id"] for row in self.checked_json("/api/projects?kind=dub", 200)["projects"]]
        self.assertEqual([key for key in listed if key in ids], ids)

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

        page = self.open_page()
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
        self.assertEqual(words, "What its audio may be used for: Commercial use allowed. Kokoro and its voice packs use Apache-2.0.")
        page = self.open_page()
        page.open(self.base + "/#projects")
        page.click("#portableImportPanel summary")
        page.set_files("#portableFile", file)
        page.click("#previewPortable")
        page.wait_until("!$('portableReview').hidden")
        self.assertIn(words, page.evaluate("$('portableMappings').textContent"))
        self.assertEqual(page.problems, [])

    @unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
    def test_home_lists_five_projects_by_last_saved_time_and_opens_each(self):
        self.start_server()
        scenes = [self.scenes.create_scene(self.scene_payload(f"Scene {n}")) for n in range(5)]
        subtitles = "\n\n".join(f"{i + 1}\n0{i}:31:00,000 --> 0{i}:31:01,000\nMira: A timed line." for i in range(3))
        parts, _ = self.scenes.import_script(
            subtitles, "srt", {"Mira": {"identity_id": self.identity["id"]}}, name="Harbor film"
        )
        book = self.checked_json(
            "/api/books",
            201,
            {
                "metadata": {"title": "Harbor book"},
                "chapters": [{"title": "Opening", "lines": [{"text": "One passage.", "speaker": ""}]}],
                "voices": {"": self.identity["id"]},
            },
        )["book"]
        # Set the fixture clock: the oldest created scene and the last subtitle part were just edited.
        with self.scenes._connect() as db:
            db.execute("UPDATE scenes SET updated_at = '2026-01-01T00:00:00.000+00:00'")
            for day, scene in zip((6, 1, 1, 2, 3), scenes, strict=True):
                db.execute(
                    "UPDATE scenes SET updated_at = ? WHERE id = ?", (f"2026-01-0{day}T00:00:00.000+00:00", scene["scene_id"])
                )
            db.execute("UPDATE scenes SET updated_at = '2026-01-04T00:00:00.000+00:00' WHERE id = ?", (parts[-1]["scene_id"],))
            db.execute("UPDATE books SET updated = '2026-01-05T00:00:00.000+00:00' WHERE id = ?", (book["id"],))
        expected = [scenes[0], book, parts[0], scenes[4], scenes[3]]
        titles = [row["name"] if "name" in row else row["metadata"]["title"] for row in expected]
        page = self.open_page()
        page.open(self.base + "/")
        for index, item in enumerate(expected):
            page.wait_until("!$('homeRecent').hidden && $('homeRecent').children.length === 5")
            self.assertEqual(page.evaluate("Array.from($('homeRecent').querySelectorAll('a'), a => a.textContent)"), titles)
            self.assertEqual(page.evaluate("$('homeNewWork').innerText"), "Import a script · Narrate a document")
            page.click(f"#homeRecent li:nth-child({index + 1}) a")
            if "scene_id" in item:
                page.wait_until(f"location.hash === '#scenes' && activeSceneId === {json.dumps(item['scene_id'])}")
            else:
                page.wait_until(f"location.hash === '#books' && activeBook?.id === {json.dumps(item['id'])}")
            page.show("home")
        self.assertEqual(page.problems, [])

    @unittest.skipUnless(HAS_FFMPEG, "needs FFmpeg to attach a synthetic video")
    def test_diagnostics_counts_workspaces_without_reporting_any_private_values(self):
        self.engines.engine_status = lambda: {"device": "cpu", "busy": False, "engines": [], "speed": []}
        self.start_server()
        private_path = Path.home() / "Private project folder" / "Only my notes.txt"
        profile_address = "https://private-provider.example/v1"
        profile_key = "fixture-private-provider-key"
        video_name = "Never share this video.mp4"
        book_title = "Unannounced book title"
        recipe_name = "Private production recipe"
        self.scenes.create_scene(self.scene_payload("Private ordinary scene"))
        timed, _ = self.scenes.import_script(
            "1\n00:00:00,400 --> 00:00:00,810\nMira: A private caption.",
            "srt",
            {"Mira": {"identity_id": self.identity["id"]}},
        )
        video = self.root / video_name
        dubbing._ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", video
        )
        source = self.checked_json(
            f"/api/scenes/{timed[0]['scene_id']}/dub/source?name=Never%20share%20this%20video.mp4",
            201,
            video.read_bytes(),
        )
        self.assertEqual(source["asset"]["name"], video_name)
        self.cast.add_reference(
            self.identity["id"],
            "source.wav",
            tone_wav(),
            transcript="Another private transcript.",
            origin=str(private_path),
            allowed_use="Synthetic test fixture",
        )
        self.checked_json(
            "/api/books",
            201,
            {
                "metadata": {"title": book_title},
                "chapters": [{"title": "Opening", "lines": [{"text": "A private chapter.", "speaker": ""}]}],
                "voices": {"": self.identity["id"]},
            },
        )
        self.checked_json(
            "/api/automation/recipes",
            201,
            {
                "schema_version": 1,
                "name": recipe_name,
                "input_kind": "text",
                "steps": [{"operation": "speak", "settings": {"voice": "test"}}],
            },
        )
        self.checked_json("/api/audio-tools/sound", 201, {"description": "Rain", "seconds": 1})
        with (
            mock.patch.dict("os.environ", {"VOICE_LAB_PROVIDER_FILE": ""}),
            mock.patch.object(connector_profiles, "SETTINGS", self.root / "providers.json"),
            mock.patch.object(connector_profiles, "_session_keys", {}),
            mock.patch(
                "voice_lab.system_report.asr_installation.status", return_value={"models": {"tiny": True, "small": False}}
            ),
        ):
            connector_profiles.save(
                {"id": "provider-private", "base_url": profile_address, "model": "example", "capabilities": ["translate"]},
                profile_key,
            )
            report = self.checked_json("/api/system", 200)
        text = report["diagnostics"]
        self.assertEqual(
            {key: report["library"]["counts"][key] for key in ("dubs", "narrations", "projects", "audio_tools", "recipes")},
            {"dubs": 1, "narrations": 1, "projects": 3, "audio_tools": 1, "recipes": 1},
        )
        self.assertIn("Provider profiles: set up", text)
        self.assertIn("Speech recognition: installed", text)
        self.assertIn("Other apps on this computer can find this Lab: no", text)
        for private in (
            self.character["name"],
            self.identity["label"],
            self.reference["transcript"],
            "Another private transcript.",
            str(private_path),
            private_path.name,
            video_name,
            book_title,
            recipe_name,
            profile_address,
            profile_key,
            self.base,
        ):
            self.assertNotIn(private, text)
        self.assertNotIn(profile_address, json.dumps(report))
        self.assertNotIn(profile_key, json.dumps(report))

    @unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
    def test_system_shows_the_full_report_and_links_to_discussions(self):
        self.engines.engine_status = lambda: {"device": "cpu", "busy": False, "engines": [], "speed": []}
        self.start_server()
        page = self.open_page()
        page.open(self.base + "/#system")
        page.wait_until("$('systemDiagnostics').textContent.includes('Speech recognition:')")
        self.assertEqual(page.evaluate("$('systemFeedback').href"), f"https://github.com/{REPOSITORY}/discussions")
        self.assertEqual(page.evaluate("$('systemFeedback').target"), "_blank")
        self.assertFalse(page.evaluate("$('systemDiagnostics').closest('details').open"))
        page.click("details:has(#systemDiagnostics) summary")
        page.wait_until("$('systemDiagnostics').closest('details').open")
        visible = page.evaluate("$('systemDiagnostics').innerText")
        self.assertEqual(visible, page.evaluate("$('systemDiagnostics').textContent"))
        self.assertIn("0 dubs, 0 narrations, 0 projects, 0 audio tool files, 0 recipes", visible)
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
