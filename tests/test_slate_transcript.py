"""A Slate exchange document retains source timing and speaker turns on its way into a timed scene."""

import json
import urllib.request

from tests.test_export_panel import ExportPanelBase


class SlateTranscriptTests(ExportPanelBase):
    def post(self, path, value):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(value).encode("utf-8"), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_exchange_import_preserves_source_and_edited_clocks_and_annotations(self):
        self.start_server()
        turns = [{"speaker_id": "fixture", "start_ms": 200, "end_ms": 2200, "timing": "estimated"}]
        media = {"sha256": "a" * 64, "kind": "audio", "duration_ms": 3000}
        words = [{"text": "Hello", "start_ms": 350, "end_ms": 700, "timing": "measured"}]
        content = json.dumps(
            {
                "schema": "fels-voice-transcript-v1",
                "source_media": media,
                "language": "en",
                "provenance": {
                    "producer": "Slate fixture",
                    "version": "1",
                    "method": "asr",
                    "model": {"id": "fixture", "revision": "sha"},
                },
                "speakers": [{"id": "fixture", "label": "Mira"}],
                "speaker_turns": turns,
                "cues": [
                    {
                        "id": "original-42",
                        "start_ms": 300,
                        "end_ms": 1400,
                        "timing": "measured",
                        "source_text": "Hello, there.",
                        "speaker_id": "fixture",
                        "words": words,
                        "edit": {"text": "Hello, friend.", "start_ms": 400, "end_ms": 1350},
                    }
                ],
            }
        )
        params = {"format": "transcript", "content": content}
        status, preview = self.post("/api/scenes/import/preview", params)
        self.assertEqual(status, 200)
        self.assertEqual(self.scenes.list_scenes(), [])
        self.assertEqual(preview["script"]["speakers"][0]["name"], "Mira")
        params["speakers"] = {"Mira": {"identity_id": self.identity["id"]}}
        status, response = self.post("/api/scenes/import", params)
        self.assertEqual(status, 201)
        scene = self.scenes.get_scene(response["scene"]["scene_id"])
        cue = scene["lines"][0]
        self.assertEqual((cue["line_id"], cue["text"]), ("original-42", "Hello, friend."))
        self.assertEqual((cue["cue"]["start_ms"], cue["cue"]["source_start_ms"]), (400, 300))
        self.assertEqual(scene["timeline"]["source_cues"][0]["text"], "Hello, there.")
        self.assertEqual(scene["source_media"], media)
        self.assertEqual(scene["transcript_source"]["speaker_turns"], turns)
        self.assertEqual(scene["transcript_source"]["words"]["original-42"], words)
