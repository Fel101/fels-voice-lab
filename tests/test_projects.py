"""Portable projects and voice files round-trip through the local Lab."""

import base64
import io
import json
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
import zipfile

import numpy as np
import soundfile as sf

from cast_store import CastStore
from scene_store import SceneStore
from tests.test_export_panel import ExportPanelBase
import portable_projects


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
