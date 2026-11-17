"""Timed transcript, real FFmpeg and existing Cast/queue through the dubbing API."""

import hashlib
import json
import time
import unittest
import urllib.error
import urllib.request

import numpy as np
import soundfile as sf

import dubbing
from tests.test_export_panel import HAS_FFMPEG, ExportPanelBase, tone_wav

SRT = """1
00:00:00,400 --> 00:00:00,810
Mira: Call the harbor.

2
00:00:00,900 --> 00:00:01,600
Mira: Hold steady.
"""


class DubbingTests(ExportPanelBase):
    def scene(self):
        manifest, _ = self.scenes.script_manifest(SRT, "srt", {"Mira": {"identity_id": self.identity["id"]}})
        return self.scenes.create_scene(manifest)

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

    def render(self, scene_id, line_ids=None):
        job = self.scenes.create_job(scene_id, line_ids)
        if not self.runner:
            from scene_store import SceneBatchRunner

            self.runner = SceneBatchRunner(self.scenes, self.base)
            self.runner.start()
        self.runner.wake()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            current = self.scenes.get_job(job["id"])
            if current["status"] == "completed":
                break
            self.assertNotEqual(current["status"], "failed", current)
            time.sleep(0.025)
        else:
            self.fail("Timed render did not finish.")
        for row in current["rows"]:
            self.scenes.select_take(scene_id, row["line_id"], row["take_id"], row["segment_id"])
        return self.scenes.get_scene(scene_id), current

    def test_video_delivery_with_fit_captions_stems_ranges_and_failed_rerun(self):
        self.start_server()
        source = self.root / "fixture.mp4"
        dubbing._ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            source,
        )
        raw = source.read_bytes()
        scene = self.scene()
        scene_id = scene["scene_id"]
        url = f"/api/scenes/{scene_id}/dub"
        uploaded = self.checked_json(url + "/source?name=fixture.mp4", 201, raw)["asset"]
        self.assertEqual(uploaded["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(uploaded["picture_copied"])
        self.assertEqual(len(uploaded["waveform"]), 480)
        self.assertEqual(self.checked_json(url + "/source?name=fixture.mp4", 201, raw)["asset"], uploaded)
        self.checked_json(url + "/source?name=changed.mp4", 400, raw + b"modified")
        self.checked_json(url + "/background?name=bed.wav", 201, tone_wav(2.0, 48_000, 0.03))
        self.assertEqual(
            self.request(url + "/source/original.mp4", headers={"Range": "bytes=0-63"})[0:3:2],
            (206, dubbing.path_for(self.scenes, scene_id, "original.mp4").read_bytes()[:64]),
        )
        self.assertEqual(self.request(url + "/source/original.mp4", headers={"Range": "bytes=99999999-"})[0], 416)

        scene, _ = self.render(scene_id)
        saved = scene["lines"][1]["selected_take_id"]
        scene["lines"][0].update(text="Llama al puerto.", language="Spanish", dub={"fit": "fit", "speed": 1, "gain_db": -2})
        scene = self.scenes.update_scene(scene_id, scene, expected_revision=scene["revision"])
        self.assertFalse(scene["lines"][0]["selected_takes"])
        self.assertEqual(scene["lines"][1]["selected_take_id"], saved)
        scene, job = self.render(scene_id, [scene["lines"][0]["line_id"]])
        self.assertEqual(len(job["rows"]), 1)
        args = {
            "expected_revision": scene["revision"],
            "target_language": "es",
            "keep_original": True,
            "background": True,
            "background_gain_db": -3,
            "duck": True,
        }
        delivery = self.checked_json(url + "/exports", 201, args)["delivery"]
        self.assertTrue(delivery["picture_copied"])
        self.assertEqual(delivery["duration_seconds"], 2.0)
        adjustments = delivery["timing"]["adjustments"]
        self.assertEqual(adjustments[0]["applied_speed"], 1.25)
        self.assertNotIn("overrun", {warning["code"] for warning in delivery["timing"]["warnings"]})
        self.assertEqual(delivery["timing"]["placements"][0]["start_sample"], 9600)
        self.assertEqual(delivery["timing"]["placements"][0]["end_sample"], 19200)
        self.assertEqual(delivery["streams"][1]["language"], "spa")
        self.assertTrue(delivery["streams"][1]["default"])
        self.assertFalse(delivery["streams"][2]["default"])
        repeated = self.checked_json(url + "/exports", 201, args)["delivery"]
        self.assertEqual(repeated["id"], delivery["id"])
        delivery_url = url + "/exports/" + delivery["id"]
        self.assertEqual(self.request(delivery_url + "/dub.mp4", headers={"Range": "bytes=-127"})[0], 206)
        self.assertIn("Llama al puerto.\nCall the harbor.", self.request(delivery_url + "/bilingual.vtt")[2].decode())
        self.assertIn("00:00:00,400 --> 00:00:00,800", self.request(delivery_url + "/target.srt")[2].decode())
        self.assertIn("00:00:00,400 --> 00:00:00,810", self.request(delivery_url + "/source.srt")[2].decode())
        out_path = dubbing.path_for(self.scenes, scene_id, "dub.mp4", delivery["id"])

        def picture(path):
            return dubbing._ffmpeg("-i", path, "-map", "0:v:0", "-c:v", "copy", "-f", "hash", "pipe:1")

        self.assertEqual(picture(source), picture(out_path))
        wav = dubbing.path_for(self.scenes, scene_id, "dub.wav", delivery["id"])
        audio, rate = sf.read(wav, always_2d=True)
        self.assertEqual((audio.shape, rate), ((96000, 2), 48000))
        self.assertLessEqual(np.max(np.abs(audio)), 0.892)
        self.assertTrue(np.any(audio[:12000]))
        self.assertTrue(self.checked_json(delivery_url + "/verify", 200, {})["ok"])

        burned = self.checked_json(url + "/exports", 201, {**args, "burn": "bilingual"})["delivery"]
        self.assertFalse(burned["picture_copied"])
        self.assertNotEqual(picture(source), picture(dubbing.path_for(self.scenes, scene_id, "dub.mp4", burned["id"])))
        scene["lines"][1]["cue"].update(start_ms=1900, end_ms=1995)
        scene = self.scenes.update_scene(scene_id, scene, expected_revision=scene["revision"])
        self.assertIn(
            "beyond the picture",
            self.checked_json(url + "/exports", 400, {**args, "expected_revision": scene["revision"]})["error"],
        )
        self.assertEqual(self.checked_json(url, 200)["delivery"]["id"], burned["id"])
        wav.write_bytes(b"corrupt")
        self.assertIn("SHA-256", self.checked_json(delivery_url + "/verify", 400, {})["error"])

    @unittest.skipUnless(HAS_FFMPEG, "Requires FFmpeg and ffprobe")
    def test_transcript_source_hash_and_subtitle_only_audio(self):
        self.start_server()
        audio = tone_wav(seconds=2.0)
        source = {"sha256": hashlib.sha256(audio).hexdigest(), "duration_ms": 2000, "kind": "audio"}
        manifest, _ = self.scenes.script_manifest(SRT, "srt", {"Mira": {"identity_id": self.identity["id"]}})
        manifest["timeline"]["format"] = "transcript"
        manifest["source_media"] = source
        scene = self.scenes.create_scene(manifest)
        scene_id = scene["scene_id"]
        url = f"/api/scenes/{scene_id}/dub"
        self.assertIn("same media file", self.checked_json(url + "/source?name=source.wav", 400, audio[:-4])["error"])
        self.assertTrue(self.checked_json(url + "/source?name=source.wav", 201, audio)["asset"]["transcript_source_verified"])
        self.assertEqual(self.request(url + "/source/original.wav")[0], 200)

        scene = self.scene()
        scene["lines"][1]["dub"] = {"mute": True}
        scene = self.scenes.update_scene(scene["scene_id"], scene, expected_revision=scene["revision"])
        line_id = scene["lines"][0]["line_id"]
        scene, _ = self.render(scene["scene_id"], [line_id])
        url = f"/api/scenes/{scene['scene_id']}/dub"
        delivery = self.checked_json(url + "/exports", 201, {"expected_revision": scene["revision"], "target_language": "en"})[
            "delivery"
        ]
        self.assertIn("dub.wav", delivery["files"])
        self.assertNotIn("dub.mp4", delivery["files"])
        self.assertTrue(delivery["timing"]["adjustments"][1]["mute"])
        self.assertIn("overrun", {warning["code"] for warning in delivery["timing"]["warnings"]})

    @unittest.skipUnless(HAS_FFMPEG, "Requires FFmpeg and ffprobe")
    def test_browser_review_attach_export_and_verify(self):
        from tests.test_browser_flow import BROWSER, DevToolsPage

        if not BROWSER:
            self.skipTest("No Chromium browser found.")
        self.start_server()
        scene = self.scene()
        scene, _ = self.render(scene["scene_id"])
        asset = self.root / "source audio.wav"
        asset.write_bytes(tone_wav(2.0))
        page = DevToolsPage(BROWSER)
        try:
            page.open(self.base + "/#scenes")
            page.wait_until("!document.getElementById('sceneDub').hidden")
            page.click("#sceneDub summary")
            page.wait_until("!document.getElementById('dubSourceFile').disabled")
            page.set_files("#dubSourceFile", asset)
            page.wait_until("document.getElementById('dubStatus').textContent === 'Media attached.'", timeout=20)
            page.click("#dubOriginal")
            page.wait_until("document.getElementById('dubAudio').duration >= 2", timeout=15)
            page.click("#dubSeekCue")
            self.assertAlmostEqual(page.evaluate("document.getElementById('dubAudio').currentTime"), 0.4, places=2)
            page.set_value("#dubTo", "es")
            page.set_value("#dubTargetText", "Llama al puerto.")
            self.assertEqual(self.scenes.get_scene(scene["scene_id"])["lines"][0]["text"], "Call the harbor.")
            page.click("#dubSaveCue")
            page.wait_until("document.getElementById('dubStatus').textContent === 'Line saved.'")
            self.assertEqual(self.scenes.get_scene(scene["scene_id"])["lines"][0]["text"], "Llama al puerto.")
            self.render(scene["scene_id"], [scene["lines"][0]["line_id"]])
            page.evaluate("refreshSceneList().then(() => true)")
            page.click("#dubExport")
            page.wait_until("document.getElementById('dubStatus').textContent === 'Dub delivery ready.'", timeout=25)
            page.click("#dubDubbed")
            page.wait_until("document.getElementById('dubAudio').src.endsWith('/dub.wav')", timeout=10)
            page.click("#dubVerify")
            page.wait_until("document.getElementById('dubStatus').textContent.endsWith('delivery files verified.')", timeout=15)
            self.assertEqual(page.problems, [])
        finally:
            page.close()
