"""Acceptance for subtitle import and exact placement of selected takes."""

import hashlib
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

import export_panel
from scene_store import SceneBatchRunner
from tools import scene_export
import timed_scene
from script_import import parse_script
from tests.test_export_panel import HAS_FFMPEG, ExportPanelBase, tone_wav


SRT = (Path(__file__).parent / "fixtures" / "scripts" / "timed-harbour.srt").read_text(encoding="utf-8")


class SubtitleParserTests(unittest.TestCase):
    def test_webvtt_keeps_cue_id_and_source_without_speaking_the_markup(self):
        source = (
            "\ufeffWEBVTT\r\n\r\nNOTE review with cast\r\n\r\nharbour-east\r\n"
            "00:02.310 --> 00:03.880 align:start\r\n<v Speaker 1><i>It’s cold</i> &amp; raining.</v>\r\n"
        )
        result = parse_script(source, "vtt")
        self.assertEqual(result["unread"], [])
        line = result["lines"][0]
        self.assertEqual((line["number"], line["speaker"], line["text"]), (5, "Speaker 1", "It’s cold & raining."))
        self.assertEqual(line["cue"]["source_id"], "harbour-east")
        self.assertEqual((line["cue"]["start_ms"], line["cue"]["end_ms"]), (2310, 3880))
        self.assertIn("<i>", line["cue"]["source_text"])


class TimedSceneTests(ExportPanelBase):
    def post(self, path, data):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_bad_cue_is_reported_at_its_source_line_and_import_creates_nothing(self):
        self.start_server()
        source = SRT + "\n\n\n5\n00:00:07,000 --> 00:00:06,000\nMira: Broken."
        data = {"format": "srt", "content": source}
        status, reply = self.post("/api/scenes/import/preview", data)
        self.assertEqual(status, 200)
        self.assertEqual(len(reply["script"]["lines"]), 4)
        self.assertEqual(reply["script"]["unread"][0]["number"], source.splitlines().index("5") + 1)
        status, _ = self.post("/api/scenes/import", data)
        self.assertEqual(status, 400)
        self.assertEqual(self.scenes.list_scenes(), [])

    def test_feature_length_subtitles_import_with_local_clocks_and_original_captions(self):
        self.start_server()
        short = [f"Mira: Line {index}" for index in range(500)]
        source = "\n\n".join(f"{index}\n00:00:01,000 --> 00:00:02,000\n{text}" for index, text in enumerate(short, 1))
        source += "\n\n501\n00:00:05,000 --> 00:00:06,000\nMira: Next."
        source += "\n\n502\n01:29:55,000 --> 01:30:00,000\nMira: The end."
        request = {"format": "srt", "content": source, "speakers": {"Mira": {"identity_id": self.identity["id"]}}}
        status, result = self.post("/api/scenes/import/preview", request)
        self.assertEqual(status, 200, result)
        self.assertEqual([part["cues"] for part in result["script"]["sections"]], [500, 1, 1])
        self.assertIn("Line 2 (00:01.000)", result["script"]["warnings"][0]["message"])
        status, result = self.post("/api/scenes/import", request)
        self.assertEqual(status, 201, result)
        first, middle, last = result["scenes"]
        self.assertEqual([item["timeline"]["source_offset_ms"] for item in (first, middle, last)], [1000, 5000, 5_395_000])
        self.assertEqual(last["lines"][0]["cue"]["start_ms"], 0)
        _, timing = timed_scene.mix(last["timeline"], [(last["lines"][0], [np.ones(24_000, dtype=np.float32)])])
        self.assertEqual(timing["video_start"], "01:29:55.000")
        self.assertIn("01:29:55,000 --> 01:30:00,000", scene_export.source_caption_text(last)[0])
        last["timeline"]["source_offset_ms"] = 0
        status, result = self.post("/api/scenes/" + last["scene_id"], last)
        self.assertEqual(status, 400, result)
        self.assertIn("Import the new subtitles", result["error"])

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg and ffprobe are needed for package verification")
    def test_four_cues_three_voices_share_the_same_sample_clock_in_preview_and_export(self):
        self.start_server()
        cast = {"Mira": self.identity}
        for speaker in ("Tobin", "Rhea"):
            other = self.cast.create_character(speaker, "")
            voice = self.cast.create_identity(other["id"], "qwen3-tts-1.7b-base", speaker)
            reference = self.cast.add_reference(
                voice["id"],
                f"{speaker}.wav",
                tone_wav(),
                transcript="Local test reference.",
                origin="synthetic fixture",
                allowed_use="tests only",
            )
            cast[speaker] = self.cast.update_identity(
                voice["id"], {"status": "approved", "reference_version_id": reference["id"]}
            )
        content = SRT.replace("Tobin: The storm", "Rhea: The storm")
        request = {
            "format": "srt",
            "content": content,
            "speakers": {speaker: {"identity_id": voice["id"]} for speaker, voice in cast.items()},
        }
        status, result = self.post("/api/scenes/import", request)
        self.assertEqual(status, 201, result)
        scene = result["scene"]
        scene_id = scene["scene_id"]
        self.assertEqual(scene["timeline"]["source_sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest())
        self.assertEqual(
            [line["identity_id"] for line in scene["lines"]], [cast[name]["id"] for name in ("Mira", "Tobin", "Mira", "Rhea")]
        )
        parsed_csv = self.scenes.import_csv(self.scenes.export_csv(scene_id))
        self.assertEqual(parsed_csv["timeline"], scene["timeline"])
        self.assertEqual([row["cue"] for row in parsed_csv["lines"]], [row["cue"] for row in scene["lines"]])

        job = self.scenes.create_job(scene_id)
        self.runner = SceneBatchRunner(self.scenes, self.base)
        self.runner.start()
        deadline = time.monotonic() + 15
        while True:
            current = self.scenes.get_job(job["id"])
            if current["status"] == "completed":
                break
            if current["status"] == "failed" or time.monotonic() >= deadline:
                self.fail(f"Timed scene render failed: {current}")
            time.sleep(0.025)
        for row in current["rows"]:
            self.scenes.select_take(scene_id, row["line_id"], row["take_id"], row["segment_id"])

        edit = self.scenes.get_scene(scene_id)
        edit["lines"][2]["cue"]["start_ms"] = 3125
        take_selection = edit["lines"][2]["selected_takes"]
        saved = self.scenes.update_scene(scene_id, edit, expected_revision=edit["revision"])
        self.assertEqual(saved["lines"][2]["selected_takes"], take_selection)
        self.assertEqual(saved["lines"][2]["cue"]["source_start_ms"], 3000)
        status, result = self.post(f"/api/scenes/{scene_id}/preview", {})
        self.assertEqual(status, 201, result)
        preview = result["preview"]
        self.assertEqual({warning["code"] for warning in preview["timing"]["warnings"]}, {"overlap", "overrun"})
        self.assertEqual([row["start_sample"] for row in preview["timing"]["placements"]], [24000, 30000, 75000, 108000])

        status, result = self.post("/api/exports", {"scene_id": scene_id, "name": "timed-scene", "formats": ["wav"]})
        self.assertEqual(status, 201, result)
        name = result["export"]["name"]
        package = self.root / result["export"]["path"]
        mix, sample_rate = sf.read(package / "scene" / "scene-mix-raw.wav", dtype="int16")
        preview_samples = sf.read(preview["audio_path"], dtype="int16")[0]
        np.testing.assert_array_equal(preview_samples, mix)
        self.assertEqual((len(mix), sample_rate), (124800, 24000))
        self.assertFalse(mix[:24000].any())
        self.assertFalse(mix[42000:75000].any())
        self.assertTrue(mix[30000:36000].any())
        captions = (package / "scene" / "scene.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:01,500", captions)
        self.assertIn("00:00:03,125 --> 00:00:03,625", captions)
        self.assertTrue((package / "scene" / "scene.vtt").read_text(encoding="utf-8").startswith("WEBVTT"))
        source_path = package / "scene" / "scene-source.srt"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:01,400", source)
        self.assertIn("00:00:03,000 --> 00:00:03,900", source)
        self.assertIn("Rhea: The storm is almost here.", source)
        self.assertTrue(export_panel.verify(self.root, name)["ok"])
        source_path.write_text(source.replace("00:00:03,900", "00:00:03,905"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source caption"):
            export_panel.verify(self.root, name)


class UntimedSceneTests(unittest.TestCase):
    def test_raw_mix_matches_the_pre_subtitle_wav_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            take = root / "take.wav"
            sf.write(take, np.array([4096, -4096, 8192, -8192], dtype=np.int16), 24_000)
            selected = [
                {
                    "line": {
                        "line_id": f"line-{number}",
                        "character_id": "narrator",
                        "identity_id": "narrator-voice",
                        "text": f"Line {number}",
                        "pause_before_ms": before,
                        "pause_after_ms": after,
                    },
                    "takes": [
                        {
                            "id": "take",
                            "segment_id": "single",
                            "character_name": "Narrator",
                            "media_path": take,
                            "probe": {"sample_rate": 24_000},
                        }
                    ],
                }
                for number, (before, after) in enumerate(((1, 2), (3, 4)), 1)
            ]
            output = root / "mix.wav"
            scene_export.make_scene_mix(root, selected, output)
            # Captured with the same inputs using the parent commit's scene exporter.
            self.assertEqual(
                hashlib.sha256(output.read_bytes()).hexdigest(),
                "fc04dc64a2a080ec49e8249f6d94cc071450681ffbfae2cff7d5349ad1f65a23",
            )
