"""Cast persistence, immutable references, validation, and service-contract checks."""

import base64
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve
from cast_store import CAST_SCHEMA_VERSION, CastStore
from worker_pool import read_catalog


def tone_wav(seconds=1.0, sample_rate=24_000, amplitude=0.1):
    time = np.arange(round(seconds * sample_rate), dtype=np.float32) / sample_rate
    audio = amplitude * np.sin(2 * np.pi * 220 * time)
    result = io.BytesIO()
    sf.write(result, audio, sample_rate, format="WAV", subtype="PCM_16")
    return result.getvalue()


class FakeEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.calls = []

    def speak(self, req):
        self.calls.append(req)
        time = np.arange(12_000, dtype=np.float32) / 24_000
        audio = 0.1 * np.sin(2 * np.pi * 260 * time)
        return audio.astype(np.float32), 24_000, {
            "total_s": 0.5, "first_chunk_s": None, "mode": req.get("mode", "clone"),
            "backend": "fake-qwen", "checkpoint": "Qwen/test", "checkpoint_revision": "test-revision",
        }


class CastStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-cast-store-")
        self.store = CastStore(self.temp.name)
        self.character = self.store.create_character("Mira", "A careful, steady identity.")
        self.identity = self.store.create_identity(self.character["id"], "qwen3-tts-1.7b-base", "Neutral")

    def tearDown(self):
        self.temp.cleanup()

    def add_reference(self, **overrides):
        values = {"transcript": "A clear reference line.", "origin": "local test fixture",
                  "allowed_use": "tests only", "edits": {"target_sample_rate": 24_000}}
        values.update(overrides)
        source = values.pop("source_bytes", tone_wav())
        return self.store.add_reference(self.identity["id"], "reference.wav", source, **values)

    def test_version_edit_does_not_mutate_source_or_approved_reference(self):
        original = self.add_reference()
        self.store.update_identity(self.identity["id"], {"status": "approved", "reference_version_id": original["id"]})
        original_source = self.store.get_media_path("reference_source", original["id"]).read_bytes()
        original_audio = self.store.get_media_path("reference", original["id"]).read_bytes()
        edited = self.store.add_reference_version(original["id"], {
            "transcript": "Edited exact transcript.", "start_seconds": 0.1, "end_seconds": 0.8,
            "target_sample_rate": 16_000, "channel": "mono",
        })
        self.assertEqual(edited["version"], 2)
        self.assertEqual(edited["parent_reference_id"], original["id"])
        self.assertEqual(self.store.get_media_path("reference_source", original["id"]).read_bytes(), original_source)
        self.assertEqual(self.store.get_media_path("reference", original["id"]).read_bytes(), original_audio)
        reopened = CastStore(self.temp.name)
        self.assertEqual(reopened.get_identity(self.identity["id"])["approved_reference_id"], original["id"])
        self.assertEqual([row["version"] for row in reopened.get_character(self.character["id"])
                          ["identities"][0]["references"]], [2, 1])

    def test_missing_transcript_silence_and_overlong_are_rejected_before_generation(self):
        with self.assertRaisesRegex(ValueError, "exact transcript"):
            self.add_reference(transcript="")
        with self.assertRaisesRegex(ValueError, "silent or too quiet"):
            self.add_reference(transcript="Quiet line.", source_bytes=tone_wav(amplitude=0.0))
        with self.assertRaisesRegex(ValueError, "60 seconds"):
            self.add_reference(transcript="Long line.", source_bytes=tone_wav(seconds=61, sample_rate=8_000))

    def test_pronunciation_dictionary_is_explicit_and_case_insensitive(self):
        self.store.save_pronunciation(character_id=self.character["id"], source="Alduin", replacement="All doo in")
        self.assertEqual(self.store.apply_pronunciations("ALDUIN returns.", self.character["id"]),
                         "All doo in returns.")
        self.assertEqual(self.store.apply_pronunciations("ALDUIN returns.", None), "ALDUIN returns.")

    def test_reference_from_take_preserves_spoken_pronunciation_and_explicit_transcript(self):
        reference = self.add_reference()
        identity = self.store.get_generation_identity(self.identity["id"], reference["id"])
        wav, rate = sf.read(io.BytesIO(tone_wav()), dtype="float32")
        take = self.store.create_take(identity, line_id="pronunciation", text="Alduin returns.",
            spoken_text="All doo in returns.", language="English", direction="", settings={},
            model={"backend":"fixture", "checkpoint_revision":"fixture"}, wav=wav,
            sample_rate=rate, generation_info={})
        derived = self.store.add_reference_from_take(self.identity["id"], take["id"])
        self.assertEqual(derived["transcript"], "All doo in returns.")
        self.assertEqual(derived["parent_take_id"], take["id"])
        self.assertEqual(derived["parent_reference_id"], reference["id"])
        corrected = self.store.add_reference_from_take(self.identity["id"], take["id"],
            transcript="Manually checked transcript.")
        self.assertEqual(corrected["transcript"], "Manually checked transcript.")
        self.assertEqual(self.store.get_take(take["id"])["text"], "Alduin returns.")


class TakeWaveformCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-take-checks-")
        self.store = CastStore(self.temp.name)
        character = self.store.create_character("Checked voice", "")
        identity = self.store.create_identity(character["id"], "qwen3-tts-1.7b-base", "Checked")
        reference = self.store.add_reference(identity["id"], "reference.wav", tone_wav(),
                                             transcript="A clear reference line.", origin="local test fixture",
                                             allowed_use="tests only", edits={"target_sample_rate": 24_000})
        self.generation = self.store.get_generation_identity(identity["id"], reference["id"])

    def tearDown(self):
        self.temp.cleanup()

    def save(self, samples, text="Hello there, friend."):
        return self.store.create_take(self.generation, line_id="checks", text=text, spoken_text=text,
                                      language="English", direction="", settings={},
                                      model={"backend": "fixture", "checkpoint_revision": "fixture"},
                                      wav=samples, sample_rate=24_000, generation_info={})

    @staticmethod
    def speech(seconds):
        time = np.arange(round(seconds * 24_000), dtype=np.float32) / 24_000
        return (0.1 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)

    @staticmethod
    def silence(seconds):
        return np.zeros(round(seconds * 24_000), dtype=np.float32)

    def test_each_waveform_problem_is_recorded_as_its_flag(self):
        cases = {
            "clean speech": ([self.speech(1.0)], "Hello there, friend.", []),
            "silent take": ([self.silence(1.0)], "Hello there, friend.", ["silent_audio"]),
            "full-scale sample": ([np.ones(24_000, dtype=np.float32)], "Hello there, friend.", ["clipping"]),
            "pause inside the take": ([self.speech(0.5), self.silence(1.5), self.speech(0.5)],
                                      "Hello there, friend.", ["long_pause"]),
            "quiet lead and tail": ([self.silence(1.0), self.speech(0.5), self.silence(2.0)],
                                    "Hello there, friend.", []),
            "cut off early": ([self.speech(0.2)], "One two three four five six seven eight nine ten.",
                              ["duration_suspiciously_short"]),
        }
        for label, (pieces, text, flags) in cases.items():
            with self.subTest(label):
                take = self.save(np.concatenate(pieces), text=text)
                self.assertEqual(take["waveform_checks"]["status"], "checked")
                self.assertEqual(take["waveform_checks"]["flags"], flags)
        pause = self.save(np.concatenate([self.speech(0.5), self.silence(1.5), self.speech(0.5)]))
        self.assertAlmostEqual(pause["waveform_checks"]["longest_pause_seconds"], 1.5, places=3)

    def test_a_failed_check_is_recorded_and_the_take_is_still_saved(self):
        with mock.patch("cast_store.take_checks", side_effect=RuntimeError("analysis unavailable")):
            take = self.save(self.speech(1.0))
        self.assertEqual(take["waveform_checks"], {"status": "unavailable", "checker": "take-waveform-v1",
                                                    "flags": [], "error": "analysis unavailable"})
        self.assertEqual(self.store.get_take(take["id"])["waveform_checks"]["status"], "unavailable")

    def test_batch_rows_read_stored_checks_for_many_takes(self):
        clean = self.save(self.speech(1.0))
        silent = self.save(self.silence(1.0))
        summaries = self.store.take_summaries_for([clean["id"], silent["id"]])
        self.assertEqual(summaries[clean["id"]]["waveform_checks"]["flags"], [])
        self.assertEqual(summaries[silent["id"]]["waveform_checks"]["flags"], ["silent_audio"])
        self.assertEqual(self.store.take_summaries_for([]), {})

    def test_a_take_reports_whether_it_has_word_times_without_carrying_the_record(self):
        take = self.save(self.speech(1.0))
        self.assertIs(take["has_word_timings"], False)
        self.assertNotIn("word_timings", take)
        record = {"schema": 1, "words": [{"word": "Hello", "start": 0.1, "end": 0.5, "probability": 0.9}]}
        self.store.set_word_timings(take["id"], record)
        for fetched in (self.store.get_take(take["id"]), self.store.list_takes()[0],
                        self.store.get_takes([take["id"]])[0]):
            self.assertIs(fetched["has_word_timings"], True)
            self.assertNotIn("word_timings", fetched)
            self.assertNotIn("word_timings_json", fetched)
        self.store.set_word_timings(take["id"], {"schema": 1, "error": "no result"})  # a failed alignment is a record too
        self.assertIs(self.store.get_take(take["id"])["has_word_timings"], True)

    def downgrade_to_schema_three(self):
        db = sqlite3.connect(self.store.db_path)
        try:
            db.execute("ALTER TABLE takes DROP COLUMN waveform_checks_json")
            db.execute("ALTER TABLE takes DROP COLUMN word_timings_json")  # schema three has neither column
            db.execute("ALTER TABLE takes DROP COLUMN phoneme_timings_json")
            db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
            db.execute("DROP INDEX takes_parent")
            db.execute("ALTER TABLE takes DROP COLUMN edit_json")
            db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
            db.execute("DROP TABLE purged_items")
            db.execute("DROP TABLE purge_batches")
            db.execute("DROP TABLE retirements")
            db.execute("PRAGMA user_version=3")
            db.commit()
        finally:
            db.close()

    def test_a_schema_step_that_dies_halfway_leaves_nothing_behind_and_the_next_open_finishes(self):
        take = self.save(self.speech(1.0))
        # Each step ends by moving the version number. A crash just before that must undo the step's own changes.
        steps = {4: ("waveform_checks_json", 3), 5: ("retirements", 4), 6: ("word_timings_json", 5),
                 7: ("phoneme_timings_json", 6), 8: ("loudness_json", 7)}
        for ends_at, (absent, left_at) in steps.items():
            with self.subTest(step_to=ends_at):
                self.downgrade_to_schema_three()

                class Crash(sqlite3.Connection):
                    def execute(self, sql, *args):
                        if sql.replace(" ", "").upper() == f"PRAGMAUSER_VERSION={ends_at}":
                            raise sqlite3.OperationalError("simulated crash")
                        return super().execute(sql, *args)

                db = sqlite3.connect(self.store.db_path, factory=Crash)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "simulated crash"):
                        CastStore._migrate(db)
                finally:
                    db.close()
                after = sqlite3.connect(self.store.db_path)
                try:
                    names = {row[1] for row in after.execute("PRAGMA table_info(takes)")}
                    names |= {row[0] for row in after.execute("SELECT name FROM sqlite_master")}
                    self.assertNotIn(absent, names)
                    self.assertEqual(after.execute("PRAGMA user_version").fetchone()[0], left_at)
                finally:
                    after.close()
                reopened = CastStore(self.temp.name)  # an ordinary open runs the remaining steps
                self.assertEqual(reopened.get_take(take["id"])["id"], take["id"])
                check = sqlite3.connect(reopened.db_path)
                try:
                    self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
                finally:
                    check.close()

    def test_takes_saved_before_checks_say_not_recorded_and_the_column_is_added(self):
        take = self.save(self.speech(1.0))
        self.downgrade_to_schema_three()
        reopened = CastStore(self.temp.name)
        self.assertEqual(reopened.get_take(take["id"])["waveform_checks"],
                         {"status": "not_recorded", "checker": None, "flags": []})
        db = sqlite3.connect(reopened.db_path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
        finally:
            db.close()


class CastApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-cast-api-")
        self.store = CastStore(self.temp.name)
        character = self.store.create_character("API test character", "")
        self.identity = self.store.create_identity(character["id"], "qwen3-tts-1.7b-base", "API validation candidate")
        self.engines = FakeEngines()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(self.engines, 0, self.store))
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1], self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def post(self, path, body):
        raw = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=raw, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_http_cast_reference_approval_take_and_restart(self):
        _, char_result = self.post("/api/characters", {"name": "Mira", "description": "Warm, steady"})
        char = char_result["character"]
        _, identity_result = self.post(f"/api/characters/{char['id']}/identities", {
            "engine_id": "qwen3-tts-1.7b-base", "label": "Warm candidate"})
        identity = identity_result["identity"]
        metadata = {"filename": "capture.wav", "transcript": "My exact spoken words.",
                    "origin": "recorded locally for the test", "allowed_use": "local test only",
                    "edits": {"start_seconds": 0.05, "end_seconds": 0.95,
                              "target_sample_rate": 24_000, "channel": "mono"}}
        encoded = base64.urlsafe_b64encode(json.dumps(metadata).encode()).decode().rstrip("=")
        request = urllib.request.Request(self.base + f"/api/identities/{identity['id']}/references",
                                         data=tone_wav(), method="POST", headers={
                                             "Content-Type": "audio/wav", "X-Reference-Metadata": encoded})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 201)
            reference = json.loads(response.read().decode())["reference"]
        _, approval = self.post(f"/api/identities/{identity['id']}/update", {
            "status": "approved", "reference_version_id": reference["id"]})
        self.assertEqual(approval["identity"]["status"], "approved")
        raw = json.dumps({"identity_id": identity["id"], "line_id": "scene-01-line-01",
                          "text": "We made it home.", "language": "English",
                          "settings": {"seed": 42}}).encode()
        try:
            response_context = urllib.request.urlopen(urllib.request.Request(self.base + "/api/performance", data=raw,
                                               headers={"Content-Type": "application/json"}), timeout=10)
        except urllib.error.HTTPError as exc:
            self.fail(f"performance failed: HTTP {exc.code} {exc.read().decode('utf-8')}")
        with response_context as response:
            wav = response.read()
            take_id = response.headers["X-Take-Id"]
            self.assertTrue(wav.startswith(b"RIFF"))
            self.assertEqual(response.headers["X-Checkpoint-Revision"], "test-revision")
        self.assertEqual(len(self.engines.calls), 1)
        self.assertEqual(self.engines.calls[0]["reference_text"], "My exact spoken words.")
        take = self.store.get_take(take_id)
        self.assertEqual(take["provenance"]["allowed_use"], "local test only")
        self.assertTrue(Path(self.store.get_media_path("take", take_id)).is_file())
        self.assertEqual(CastStore(self.temp.name).get_take(take_id)["spoken_text"], "We made it home.")
        self.assertEqual(take["waveform_checks"]["status"], "checked")
        with urllib.request.urlopen(self.base + f"/api/takes/{take_id}", timeout=10) as response:
            self.assertEqual(json.loads(response.read().decode("utf-8"))["take"]["waveform_checks"]["status"], "checked")
        with urllib.request.urlopen(self.base + f"/api/takes?identity_id={identity['id']}&line_id=scene-01-line-01",
                                    timeout=10) as response:
            listed = json.loads(response.read().decode("utf-8"))["takes"]
        self.assertEqual(listed[0]["waveform_checks"]["flags"], [])

    def test_missing_reference_is_rejected_before_engine_call(self):
        char = self.store.create_character("Incomplete", "")
        identity = self.store.create_identity(char["id"], "qwen3-tts-1.7b-base", "No recording")
        body = {"identity_id": identity["id"], "line_id": "line-01", "text": "Do not start a model.",
                "language": "English", "settings": {"seed": 42}}
        request = urllib.request.Request(self.base + "/api/performance", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(rejected.exception.code, 400)
        self.assertIn("reference version", rejected.exception.read().decode("utf-8").lower())
        self.assertEqual(self.engines.calls, [])

    def test_transcript_silence_and_overlong_uploads_are_rejected_before_engine_call(self):
        cases = [
            ("Missing exact transcript", tone_wav(), {"transcript": ""}, "exact transcript"),
            ("Silent recording", tone_wav(amplitude=0.0), {"transcript": "Quiet words."}, "silent or too quiet"),
            ("Overlong recording", tone_wav(seconds=61, sample_rate=8_000),
             {"transcript": "A long passage."}, "60 seconds"),
        ]
        for name, audio, fields, message in cases:
            with self.subTest(name=name):
                metadata = {"filename": "validation.wav", "origin": "test fixture",
                            "allowed_use": "tests only", "edits": {"target_sample_rate": 24_000}}
                metadata.update(fields)
                encoded = base64.urlsafe_b64encode(json.dumps(metadata).encode()).decode().rstrip("=")
                request = urllib.request.Request(
                    self.base + f"/api/identities/{self.identity['id']}/references", data=audio,
                    method="POST", headers={"Content-Type": "audio/wav", "X-Reference-Metadata": encoded})
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(rejected.exception.code, 400)
                self.assertIn(message, rejected.exception.read().decode("utf-8").lower())
                self.assertEqual(self.engines.calls, [])


if __name__ == "__main__":
    unittest.main()
