"""Round-trip the exact source and a generated background through a portable dub."""

import base64
import json
import unittest

from tests import test_dubbing


class DubProjectMediaTests(test_dubbing.DubbingTests):
    @unittest.skipUnless(test_dubbing.HAS_FFMPEG, "Requires FFmpeg and ffprobe")
    def test_dub_source_and_generated_background_are_opt_in_for_portable_projects(self):
        self.start_server()
        scene = self.scene()
        path = f"/api/scenes/{scene['scene_id']}/dub"
        words = test_dubbing.tone_wav(2)
        first = self.checked_json(path + "/source?name=sample.wav", 201, words)["asset"]
        effect = self.checked_json("/api/audio-tools/sound", 201, {"description": "Rain", "seconds": 2})["delivery"]
        background = self.checked_json(
            f"/api/audio-tools/{effect['id']}/dub-background",
            201,
            {"scene_id": scene["scene_id"]},
        )["background"]
        request = {"kind": "dub", "id": scene["scene_id"]}
        for include in (False, True):
            archive = self.checked_json("/api/portable/export", 201, {**request, "include_media": include})["archive"]
            code, _, raw = self.request(archive["download"])
            self.assertEqual(code, 200)
            preview = self.checked_json("/api/portable/preview", 200, raw)["archive"]
            self.assertEqual(preview["media"], ["source", "background"] if include else [])
            identity = self.identity["id"]
            options = {"archive_sha256": preview["archive_sha256"], "voices": {identity: identity}, "include_media": include}
            headers = {
                "Content-Type": "application/octet-stream",
                "X-Reference-Metadata": base64.urlsafe_b64encode(json.dumps(options).encode()).decode(),
            }
            status, _, response = self.request("/api/portable/import", raw, headers)
            self.assertEqual(status, 201, response)
            project = json.loads(response)["imported"]
            media = self.checked_json(f"/api/scenes/{project['project_id']}/dub", 200)
            if include:
                self.assertEqual(media["source"]["sha256"], first["sha256"])
                self.assertEqual(media["background"]["sha256"], background["sha256"])
            else:
                self.assertIsNone(media["source"])
                self.assertIsNone(media["background"])
