"""Scripts in plain text and Fountain: the reader (script_import.py), and the preview and import routes that make a scene."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402
from cast_store import CastStore  # noqa: E402
from scene_store import SceneBatchRunner, SceneStore  # noqa: E402
from script_import import parse_script  # noqa: E402
from tests.test_scenes import FakeEngines, tone_wav  # noqa: E402

SCRIPTS = ROOT / "tests" / "fixtures" / "scripts"


def sample(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


def said(script):
    return [(line["number"], line["speaker"], line["direction"], line["text"]) for line in script["lines"]]


class FountainTests(unittest.TestCase):
    def setUp(self):
        self.script = parse_script(sample("harbour.fountain"), "fountain")

    def test_the_sample_screenplay_reads_as_its_speeches_and_its_action(self):
        self.assertEqual(self.script["title"], "The Harbour Light")
        self.assertEqual(
            self.script["speakers"],
            [{"name": "MARA", "lines": 4}, {"name": "TOMAS", "lines": 5}, {"name": "McCLANE", "lines": 1}],
        )
        self.assertEqual(
            said(self.script),
            [
                (12, None, "", "Rain against the glass. MARA (40s) trims the wick of a lantern."),
                (17, "MARA", "", "I kept the light on. Every night."),
                (20, "TOMAS", "", "Mara? Is that you up there?"),
                (24, "MARA", "calling down", "Who else would be fool enough to climb these stairs in this weather?"),
                (26, None, "", "Tomas climbs into view, soaked."),
                (29, "TOMAS", "", "I came as soon as the boat was in."),
                (31, "TOMAS", "beat", "The harbour master says the storm turns at midnight."),
                (39, "MARA", "", "Then we wait."),
                (42, "TOMAS", "", "Then we wait."),
                (46, "McCLANE", "from the radio", "Lighthouse, this is the Saint Brigid. Do you read?"),
                (48, None, "", "The radio crackles."),
                (51, "MARA", "", "I read you, Saint Brigid. Keep to the light."),
                (59, None, "", "The boat rounds the point, its lamps steady."),
                (62, "TOMAS", "", "Bring her home, bring her home."),
            ],
        )
        self.assertEqual(self.script["narration"], 4)
        self.assertEqual(self.script["unread"], [])

    def test_headings_transitions_notes_boneyard_sections_and_synopses_are_left_out(self):
        words = " ".join(line["text"] for line in self.script["lines"])
        for absent in (
            "LIGHTHOUSE - NIGHT",
            "HARBOUR WALL",
            "CUT TO",
            "ACT ONE",
            "waits for her brother",
            "flicker",
            "Nobody will ever hear this",
            "===",
            "Draft date",
            "*",
            "_",
            "~",
        ):
            self.assertNotIn(absent, words)

    def test_a_cue_needs_a_blank_row_before_it_and_words_after_it(self):
        script = parse_script(
            "Action first.\nNOT A CUE\nstill action\n\nSHOUTED ACTION.\n\nMARA\n(only a direction)\n", "fountain"
        )
        self.assertEqual(
            said(script), [(1, None, "", "Action first. NOT A CUE still action"), (5, None, "", "SHOUTED ACTION.")]
        )
        self.assertEqual(script["unread"], [{"number": 7, "text": "MARA", "reason": "a character name with no words to say"}])

    def test_forced_elements_escapes_and_a_title_spread_over_rows(self):
        script = parse_script(
            "Title:\n    _The Long_\n    **Night**\nAuthor: Someone\n\n@Mara\nTwo \\*stars\\* stay.\n  \nStill Mara.\n\n"
            "!LOUD ACTION, NOT A CUE.\n\n.FORCED HEADING\n\n> FADE OUT.\n",
            "fountain",
        )
        self.assertEqual(script["title"], "The Long Night")
        self.assertEqual(
            said(script), [(7, "Mara", "", "Two *stars* stay. Still Mara."), (11, None, "", "LOUD ACTION, NOT A CUE.")]
        )

    def test_windows_line_ends_and_a_byte_order_mark(self):
        script = parse_script("﻿MARA\r\nHello.\r\n", "fountain")
        self.assertEqual(said(script), [(2, "MARA", "", "Hello.")])


class PlainTextTests(unittest.TestCase):
    def test_the_sample_reads_names_directions_narration_and_what_it_cannot_read(self):
        script = parse_script(sample("harbour.txt"), "text")
        self.assertEqual(
            script["speakers"],
            [{"name": "MARA", "lines": 4}, {"name": "Tomas", "lines": 2}, {"name": "Radio operator", "lines": 1}],
        )
        self.assertEqual(
            said(script)[:4],
            [
                (1, None, "", "The Harbour Light, a short scene in plain text."),
                (3, "MARA", "", "I kept the light on. Every night."),
                (4, "Tomas", "calling up", "Mara? Is that you up there?"),
                (5, "MARA", "dry", "Who else would be fool enough to climb these stairs in this weather?"),
            ],
        )
        self.assertEqual(
            said(script)[6], (12, "Radio operator", "crackling", "Lighthouse, this is the Saint Brigid. Do you read?")
        )
        self.assertEqual(
            script["unread"],
            [
                {"number": 8, "text": "(They watch the water.)", "reason": "a direction with no words to say"},
                {"number": 11, "text": "Tomas:", "reason": "a name with no words to say"},
            ],
        )
        self.assertEqual(script["title"], "")

    def test_a_name_is_one_to_four_words_and_an_address_is_not_a_name(self):
        script = parse_script(
            "See https://example.com: for more\nhttps://example.com/page\n"
            "The man in the long grey coat: hello\nDr. Ruiz: Hello.\n",
            "text",
        )
        self.assertEqual([line["speaker"] for line in script["lines"]], [None, None, None, "Dr. Ruiz"])

    def test_the_same_name_in_another_case_is_the_same_speaker(self):
        script = parse_script("MARA: One.\nMara: Two.\n", "text")
        self.assertEqual(script["speakers"], [{"name": "MARA", "lines": 2}])
        self.assertEqual([line["speaker"] for line in script["lines"]], ["MARA", "MARA"])

    def test_an_unknown_format_is_refused(self):
        with self.assertRaisesRegex(ValueError, "text, fountain"):
            parse_script("MARA: Hi.", "markdown")


class ScriptImportRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-script-import-")
        self.cast = CastStore(self.temp.name)
        self.mara, self.mara_voice = self.voice("Mara", "qwen3-tts-1.7b-base")
        self.tomas, self.tomas_voice = self.voice("Tomas", "turbo")
        self.engines = FakeEngines()
        self.scenes = SceneStore(self.cast, self.engines.entries)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.runner = SceneBatchRunner(self.scenes, f"http://127.0.0.1:{self.server.server_address[1]}")
        self.server.RequestHandlerClass = serve.make_handler(
            self.engines, self.server.server_address[1], self.cast, self.scenes, self.runner
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.runner.close(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def voice(self, name, engine):
        character = self.cast.create_character(name, "")
        identity = self.cast.create_identity(character["id"], engine, f"{name} voice")
        reference = self.cast.add_reference(
            identity["id"],
            f"{name.lower()}.wav",
            tone_wav(6.0),
            transcript="A clear reference line.",
            origin="local test fixture",
            allowed_use="tests only",
            edits={"target_sample_rate": 24_000},
        )
        self.cast.update_identity(identity["id"], {"status": "approved", "reference_version_id": reference["id"]})
        return character, identity

    def post(self, path, body):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def fountain(self, **more):
        return {"format": "fountain", "content": sample("harbour.fountain"), **more}

    def test_the_preview_reads_the_script_and_creates_nothing(self):
        status, body = self.post("/api/scenes/import/preview", self.fountain())
        self.assertEqual(status, 200)
        self.assertEqual(body["script"], parse_script(sample("harbour.fountain"), "fountain"))
        self.assertEqual(self.scenes.list_scenes(), [])
        status, body = self.post("/api/scenes/import/preview", {"format": "pdf", "content": "x"})
        self.assertEqual(status, 400)

    def test_a_fountain_script_becomes_a_scene_with_the_chosen_voices_and_a_report_of_what_it_left_out(self):
        status, body = self.post(
            "/api/scenes/import",
            self.fountain(
                speakers={
                    "MARA": {"identity_id": self.mara_voice["id"]},
                    "tomas": {"identity_id": self.tomas_voice["id"]},
                    "McCLANE": None,
                }
            ),
        )
        self.assertEqual(status, 201, body)
        scene, report = body["scene"], body["report"]
        self.assertEqual(scene["name"], "The Harbour Light")
        self.assertEqual([line["line_id"] for line in scene["lines"]], [f"line-{n:03d}" for n in range(1, 10)])
        self.assertEqual(
            [(line["character_id"], line["text"][:12]) for line in scene["lines"]][:3],
            [(self.mara["id"], "I kept the l"), (self.tomas["id"], "Mara? Is tha"), (self.mara["id"], "Who else wou")],
        )
        self.assertEqual({line["line_type"] for line in scene["lines"]}, {"dialogue"})
        self.assertEqual(scene["lines"][2]["direction"], "calling down")
        self.assertEqual(
            [(line["pause_after_ms"], line["take_count"], line["settings"]) for line in scene["lines"][:1]], [(250, 1, {})]
        )
        # Chatterbox Turbo takes no acting direction: Tomas's "(beat)" is left out and listed.
        self.assertEqual(scene["lines"][4]["direction"], "")
        self.assertEqual(
            report["directions_left_out"],
            [
                {
                    "number": 31,
                    "speaker": "TOMAS",
                    "direction": "beat",
                    "text": "The harbour master says the storm turns at midnight.",
                }
            ],
        )
        self.assertEqual(report["lines"], 9)
        self.assertEqual(
            [(item["number"], item["speaker"], item["reason"]) for item in report["left_out"]],
            [
                (12, None, "no narrator voice was chosen"),
                (26, None, "no narrator voice was chosen"),
                (46, "McCLANE", "its speaker is left out"),
                (48, None, "no narrator voice was chosen"),
                (59, None, "no narrator voice was chosen"),
            ],
        )
        self.assertEqual(report["unread"], [])
        self.assertEqual(len(self.scenes.list_scenes()), 1)

    def test_a_speaker_with_no_voice_and_not_left_out_cannot_be_imported(self):
        status, body = self.post("/api/scenes/import", self.fountain(speakers={"MARA": {"identity_id": self.mara_voice["id"]}}))
        self.assertEqual(status, 400)
        self.assertIn("Choose a voice for each speaker, or leave their lines out: TOMAS, McCLANE.", body["error"])
        for speakers in (
            None,
            {"MARA": None, "TOMAS": None, "McCLANE": None},
            {"MARA": None, "TOMAS": None, "McCLANE": None, "NOBODY": None},
            {"MARA": {"identity_id": "0" * 32}, "TOMAS": None, "McCLANE": None},
            {"MARA": "not an object", "TOMAS": None, "McCLANE": None},
        ):
            status, body = self.post("/api/scenes/import", self.fountain(speakers=speakers))
            self.assertEqual(status, 400, (speakers, body))
        self.assertEqual(self.scenes.list_scenes(), [])

    def test_a_narrator_voice_speaks_the_lines_with_no_name_and_a_name_can_be_given(self):
        status, body = self.post(
            "/api/scenes/import",
            {
                "format": "text",
                "content": sample("harbour.txt"),
                "name": "  Harbour,   plain  ",
                "speakers": {
                    "MARA": {"identity_id": self.mara_voice["id"]},
                    "Tomas": {"identity_id": self.tomas_voice["id"]},
                    "Radio operator": {"identity_id": self.tomas_voice["id"]},
                },
                "narrator": {"identity_id": self.mara_voice["id"]},
            },
        )
        self.assertEqual(status, 201, body)
        scene = body["scene"]
        self.assertEqual(scene["name"], "Harbour, plain")
        self.assertEqual(len(scene["lines"]), 8)
        self.assertEqual(
            (scene["lines"][0]["line_type"], scene["lines"][0]["identity_id"]), ("narration", self.mara_voice["id"])
        )
        self.assertEqual(body["report"]["left_out"], [])
        self.assertEqual([item["number"] for item in body["report"]["unread"]], [8, 11])


if __name__ == "__main__":
    unittest.main()
