"""The take-editing routes over HTTP, on the real handler with a temporary library: the waveform that draws a take, a preview that
saves nothing, a save that makes a new take, and what each refuses. None of them reaches an engine."""
import io
import json
import sys
import threading
import unittest
from pathlib import Path

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_spec
import take_edit
from api_service import RATE, Service
from audio_fixtures import speech_between_silences

TEXT = "Hold the line now"
MISSING = "0" * 32


class EditRouteCase(unittest.TestCase):
    """A service with one saved take: speech from 0.4 s to 1.6 s of 2.0 s."""

    def setUp(self):
        self.service = Service(self)
        self.identity = self.service.add_voice()
        self.parent = self.service.cast.create_take(
            self.service.cast.get_generation_identity(self.identity["id"]), line_id="line-01", text=TEXT, spoken_text=TEXT,
            language="English", direction="", settings={"speed": 1.0},
            model={"backend": "fixture", "checkpoint_revision": "fixture"}, wav=speech_between_silences(), sample_rate=RATE,
            generation_info={})
        self.url = f"/api/takes/{self.parent['id']}"

    def post(self, suffix, body):
        return self.service.request("POST", self.url + suffix, body)

    def post_json(self, suffix, body):
        status, _headers, raw = self.post(suffix, body)
        return status, json.loads(raw)

    def takes(self):
        return self.service.get_json("/api/takes")["takes"]


class WaveformRouteTests(EditRouteCase):
    def test_it_describes_the_take(self):
        wave = self.service.get_json(self.url + "/waveform")
        self.assertEqual((wave["duration_seconds"], wave["sample_rate"], wave["points"], len(wave["peaks"])), (2.0, RATE, 600, 600))
        self.assertAlmostEqual(wave["speech"]["start_seconds"], 0.4, delta=0.03)
        self.assertAlmostEqual(wave["speech"]["end_seconds"], 1.6, delta=0.03)
        self.assertEqual(wave["speech"]["threshold_db"], take_edit.SUGGESTED_THRESHOLD_DB)
        self.assertEqual(self.service.get_json(self.url + "/waveform?points=50")["points"], 50)

    def test_a_number_of_points_outside_the_range_is_refused(self):
        for query in ("points=9", "points=2001", "points=abc", "points=-5", "points=1.5"):
            with self.subTest(query):
                status, _headers, raw = self.service.request("GET", f"{self.url}/waveform?{query}")
                self.assertEqual(status, 400)
                self.assertIn("points must be a whole number from 10 to 2000", json.loads(raw)["error"])

    def test_a_take_that_does_not_exist_is_not_found(self):
        status, _headers, raw = self.service.request("GET", f"/api/takes/{MISSING}/waveform")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(raw))


class PreviewRouteTests(EditRouteCase):
    def test_it_answers_the_edited_audio_and_saves_nothing(self):
        status, headers, raw = self.post("/edit-preview", {"start_seconds": 0.3, "end_seconds": 1.8, "gain_db": 3, "fade_in_ms": 20})
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/wav"))
        samples, rate = sf.read(io.BytesIO(raw), dtype="int16")
        self.assertEqual((rate, len(samples)), (RATE, 36000))
        self.assertEqual((headers["X-Audio-Seconds"], headers["X-Edit-Start-Seconds"], headers["X-Edit-End-Seconds"]),
                         ("1.50", "0.300", "1.800"))
        self.assertAlmostEqual(float(headers["X-Peak-Dbfs"]), -7.46, delta=0.05)
        self.assertEqual([take["id"] for take in self.takes()], [self.parent["id"]])

    def test_the_documented_headers_are_the_ones_it_sends(self):
        route = next(row for row in api_spec.ROUTES if row.path == "/api/takes/{take_id}/edit-preview")
        _status, headers, _raw = self.post("/edit-preview", {"gain_db": 1})
        for field in route.headers:
            self.assertIn(field.name, headers)

    def test_a_silent_result_has_no_peak(self):
        status, headers, raw = self.post("/edit-preview", {"start_seconds": 0.0, "end_seconds": 0.3})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Peak-Dbfs"], "")
        self.assertFalse(sf.read(io.BytesIO(raw), dtype="int16")[0].any())

    def test_auto_trim_reports_where_it_cut(self):
        _status, headers, raw = self.post("/edit-preview", {"auto_trim": True, "keep_silence_ms": 0})
        seconds = len(sf.read(io.BytesIO(raw), dtype="int16")[0]) / RATE
        self.assertAlmostEqual(seconds, 1.2, delta=0.04)
        self.assertAlmostEqual(float(headers["X-Edit-Start-Seconds"]), 0.4, delta=0.03)
        self.assertAlmostEqual(float(headers["X-Edit-End-Seconds"]), 1.6, delta=0.03)


class SaveRouteTests(EditRouteCase):
    def test_it_makes_a_new_take_and_answers_201(self):
        status, body = self.post_json("/edit", {"start_seconds": 0.3, "end_seconds": 1.8, "fade_out_ms": 30})
        self.assertEqual(status, 201)
        take = body["take"]
        self.assertFalse(body["replayed"])
        self.assertEqual((take["parent_take_id"], take["status"]), (self.parent["id"], "candidate"))
        self.assertEqual(take["edit"]["request"]["fade_out_ms"], 30.0)
        self.assertEqual([item["id"] for item in self.takes()], [self.parent["id"], take["id"]])
        status, _headers, wav = self.service.request("GET", take["audio_url"])
        self.assertEqual((status, len(sf.read(io.BytesIO(wav), dtype="int16")[0])), (200, 36000))
        self.assertEqual(self.service.get_json(self.url)["take"], self.service.cast.get_take(self.parent["id"]))

    def test_the_same_key_gives_the_take_that_was_made(self):
        body = {"start_seconds": 0.3, "gain_db": 2, "idempotency_key": "save-1"}
        first_status, first = self.post_json("/edit", body)
        again_status, again = self.post_json("/edit", dict(body))
        self.assertEqual((first_status, first["replayed"], again_status, again["replayed"]), (201, False, 200, True))
        self.assertEqual(again["take"]["id"], first["take"]["id"])
        self.assertEqual(len(self.takes()), 2)

    def test_a_key_used_for_a_different_edit_is_refused(self):
        self.post_json("/edit", {"start_seconds": 0.3, "idempotency_key": "save-1"})
        status, body = self.post_json("/edit", {"start_seconds": 0.4, "idempotency_key": "save-1"})
        self.assertEqual(status, 400)
        self.assertIn("already used with different inputs", body["error"])
        self.assertEqual(len(self.takes()), 2)

    def test_a_key_an_edit_used_is_not_a_performances_replay(self):
        self.post_json("/edit", {"start_seconds": 0.3, "idempotency_key": "save-1"})
        same_inputs = {"identity_id": self.identity["id"], "line_id": "line-01", "text": TEXT, "idempotency_key": "save-1"}
        status, headers, raw = self.service.request("POST", "/api/performance", same_inputs)
        self.assertEqual(status, 400)
        self.assertIn("already used with different inputs", json.loads(raw)["error"])
        self.assertIsNone(headers.get("X-Idempotent-Replay"))
        self.assertEqual(self.service.engines.requests, [])

    def test_a_key_that_is_not_one_to_two_hundred_characters_of_text_is_refused(self):
        for key in (5, "", "x" * 201, ["a"]):
            with self.subTest(key=key):
                status, body = self.post_json("/edit", {"start_seconds": 0.3, "idempotency_key": key})
                self.assertEqual(status, 400)
                self.assertIn("idempotency key must contain", body["error"])
        self.assertEqual(len(self.takes()), 1)

    def test_a_key_is_not_an_edit_field_and_a_misspelt_field_is_refused(self):
        status, body = self.post_json("/edit", {"start_seconds": 0.3, "idempotency_keys": "save-1"})
        self.assertEqual(status, 400)
        self.assertIn("Unknown edit field 'idempotency_keys'", body["error"])


class RefusalTests(EditRouteCase):
    CASES = (
        ({}, "changes nothing"),
        ({"gain": 3}, "Unknown edit field 'gain'"),
        ({"gain_db": "3"}, "gain_db must be a number"),
        ({"auto_trim": "yes"}, "auto_trim must be true or false"),
        ({"gain_db": 24.5}, "gain_db must be from -60 to 24"),
        ({"gain_db": 24}, "would clip"),
        ({"start_seconds": 1.5, "end_seconds": 1.0}, "end_seconds must be after start_seconds"),
        ({"start_seconds": 0.5, "end_seconds": 0.52}, "shorter than 0.05 seconds"),
        ({"fade_in_ms": 1500, "fade_out_ms": 1500}, "fades are longer than the edited take"),
        ({"auto_trim": True, "start_seconds": 0.0, "end_seconds": 0.3}, "louder than -50 dB"),
    )

    def test_a_preview_and_a_save_refuse_the_same_things_and_save_nothing(self):
        for body, message in self.CASES:
            for suffix in ("/edit-preview", "/edit"):
                with self.subTest(suffix=suffix, body=body):
                    status, _headers, raw = self.post(suffix, body)
                    self.assertEqual(status, 400)
                    self.assertIn(message, json.loads(raw)["error"])
        self.assertEqual([take["id"] for take in self.takes()], [self.parent["id"]])

    def test_a_body_that_is_not_an_object_is_refused(self):
        for suffix in ("/edit-preview", "/edit"):
            status, _headers, raw = self.post(suffix, [])
            self.assertEqual(status, 400)
            self.assertIn("JSON object", json.loads(raw)["error"])

    def test_a_take_that_does_not_exist_is_not_found(self):
        for suffix in ("/edit-preview", "/edit"):
            status, _headers, raw = self.service.request("POST", f"/api/takes/{MISSING}{suffix}", {"gain_db": 1})
            self.assertEqual(status, 404)
            self.assertIn("error", json.loads(raw))

    def test_a_retired_take_is_refused(self):
        self.service.cast.retire("take", self.parent["id"])
        for suffix in ("/edit-preview", "/edit"):
            status, _headers, raw = self.post(suffix, {"gain_db": 1})
            self.assertEqual(status, 400)
            self.assertIn("retired", json.loads(raw)["error"])

    def test_no_edit_reaches_an_engine(self):
        self.post("/edit-preview", {"gain_db": 1})
        self.post("/edit", {"gain_db": 1})
        self.service.request("GET", self.url + "/waveform")
        self.assertEqual((self.service.engines.requests, self.service.engines.health_calls), ([], 0))

    def test_an_edit_does_not_wait_for_a_generation_in_progress(self):
        engines = self.service.engines
        engines.gate = threading.Event()
        speaking = threading.Thread(target=lambda: self.service.request("POST", "/api/speak", {"text": "Hello there."}), daemon=True)
        speaking.start()
        try:
            self.assertTrue(engines.started.wait(10), "the engine never got the request")
            status, _headers, _raw = self.post("/edit-preview", {"gain_db": 1})
            self.assertEqual(status, 200)
            status, body = self.post_json("/edit", {"gain_db": 1})
            self.assertEqual(status, 201)
            self.assertEqual(body["take"]["parent_take_id"], self.parent["id"])
        finally:
            engines.gate.set()
            speaking.join(15)


class DocumentedShapeTests(unittest.TestCase):
    """The page says what an edit takes and what it can do; these fail when the engine and the page part ways."""

    def fields(self):
        return {field.name: field.doc for field in api_spec.EDIT_FIELDS}

    def test_the_documented_fields_are_the_ones_an_edit_takes(self):
        def body_of(path):
            return [field.name for field in next(row for row in api_spec.ROUTES if row.path == path).body]

        self.assertEqual(list(self.fields()), list(take_edit.FIELDS))
        self.assertEqual(body_of("/api/takes/{take_id}/edit-preview"), list(take_edit.FIELDS))
        self.assertEqual(body_of("/api/takes/{take_id}/edit"), [*take_edit.FIELDS, "idempotency_key"])

    def test_the_documented_ranges_and_defaults_are_the_real_ones(self):
        docs = self.fields()
        for name, (low, high) in (("silence_threshold_db", take_edit.THRESHOLD_DB_RANGE),
                                  ("keep_silence_ms", take_edit.KEEP_MS_RANGE), ("gain_db", take_edit.GAIN_DB_RANGE),
                                  ("fade_in_ms", (0, take_edit.MAX_FADE_MS)), ("fade_out_ms", (0, take_edit.MAX_FADE_MS))):
            self.assertIn(f"{low:g} to {high:g}", docs[name], name)
        self.assertIn(f"Default {take_edit.SUGGESTED_THRESHOLD_DB:g}", docs["silence_threshold_db"])
        self.assertIn(f"Default {take_edit.DEFAULT_KEEP_MS:g}", docs["keep_silence_ms"])
        self.assertIn(f"under {take_edit.MIN_RESULT_SECONDS:g} seconds", api_spec.EDIT_REFUSALS)

    def test_the_waveform_row_names_the_threshold_the_speech_is_found_at(self):
        route = next(row for row in api_spec.ROUTES if row.path == "/api/takes/{take_id}/waveform")
        self.assertIn(f"louder than {take_edit.SUGGESTED_THRESHOLD_DB:g} dB", route.reply)


if __name__ == "__main__":
    unittest.main()
