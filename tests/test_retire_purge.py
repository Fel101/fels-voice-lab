"""Retire, restore, and purge: new use is blocked everywhere, and removal keeps an audited record."""

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
from reference_catalog import list_references, list_voice_library
from tools.scene_export import ExportError, load_takes
from worker_pool import EnginePool, read_catalog


def speech(seconds=1.0, sample_rate=24_000):
    time_axis = np.arange(round(seconds * sample_rate), dtype=np.float32) / sample_rate
    return (0.1 * np.sin(2 * np.pi * 220 * time_axis)).astype(np.float32)


def tone_wav(seconds=1.0, sample_rate=24_000):
    result = io.BytesIO()
    sf.write(result, speech(seconds, sample_rate), sample_rate, format="WAV", subtype="PCM_16")
    return result.getvalue()


class FakeEngines:
    """Stand-in for the model broker. These tests never generate audio, so reaching it is a failure."""

    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"

    def speak(self, req):
        raise AssertionError("A retired voice must not reach an engine.")


class CastFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-retire-")
        self.root = Path(self.temp.name)
        self.store = CastStore(self.root)
        self.character = self.store.create_character("Mira", "Warm and steady")
        self.identity, self.reference = self.approved_voice(self.character["id"], "Mira base", "mira.wav")

    def tearDown(self):
        self.temp.cleanup()

    def approved_voice(self, character_id, label, filename):
        identity = self.store.create_identity(character_id, "qwen3-tts-1.7b-base", label)
        reference = self.store.add_reference(identity["id"], filename, tone_wav(), transcript="A clear reference line.",
                                             origin="local test fixture", allowed_use="tests only",
                                             edits={"target_sample_rate": 24_000})
        self.store.update_identity(identity["id"], {"status": "approved", "reference_version_id": reference["id"]})
        return identity, reference

    def make_take(self, text="Hello there, friend.", identity=None, reference=None):
        identity = identity or self.identity
        reference = reference or self.reference
        generation = self.store.get_generation_identity(identity["id"], reference["id"])
        return self.store.create_take(generation, line_id="line-01", text=text, spoken_text=text, language="English",
                                      direction="", settings={}, model={"backend": "fixture", "checkpoint_revision": "fixture"},
                                      wav=speech(), sample_rate=24_000, generation_info={})

    def active_retirements(self):
        db = sqlite3.connect(self.store.db_path)
        try:
            return db.execute("SELECT COUNT(*) FROM retirements WHERE restored_at IS NULL").fetchone()[0]
        finally:
            db.close()


class RetirementRuleTests(CastFixture):
    def test_retiring_a_voice_blocks_its_recordings_and_takes_without_deleting_them(self):
        take = self.make_take()
        self.store.retire("identity", self.identity["id"], reason="Owner withdrew permission.")
        retired_take = self.store.get_take(take["id"])
        self.assertTrue(retired_take["retired"])
        self.assertEqual(retired_take["retirement"]["source_kind"], "identity")
        self.assertTrue(retired_take["retirement"]["inherited"])
        self.assertTrue(self.store.get_reference(self.reference["id"])["retired"])
        identity = self.store.get_identity(self.identity["id"])
        self.assertTrue(identity["retired"])
        self.assertEqual(identity["status"], "candidate")  # Retirement clears approval; it is never restored silently.
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.get_generation_identity(self.identity["id"], self.reference["id"])
        self.assertTrue(self.store.get_media_path("take", take["id"]).is_file())  # Kept for the owner's review.
        self.store.restore("identity", self.identity["id"])
        self.assertFalse(self.store.get_take(take["id"])["retired"])
        self.assertEqual(self.store.get_identity(self.identity["id"])["status"], "candidate")
        self.store.get_generation_identity(self.identity["id"], self.reference["id"])

    def test_retiring_a_recording_blocks_its_takes_and_versions_and_clears_approval(self):
        version = self.store.add_reference_version(self.reference["id"], {"transcript": "Edited exact transcript."})
        take = self.make_take()
        self.store.retire("reference", self.reference["id"], reason="Wrong recording.")
        self.assertTrue(self.store.get_take(take["id"])["retired"])
        self.assertTrue(self.store.get_reference(version["id"])["retired"])
        identity = self.store.get_identity(self.identity["id"])
        self.assertEqual((identity["status"], identity["approved_reference_id"]), ("candidate", None))
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.add_reference_version(self.reference["id"], {"transcript": "Another edit."})
        self.store.restore("reference", self.reference["id"])
        self.assertFalse(self.store.get_reference(self.reference["id"])["retired"])
        self.assertFalse(self.store.get_take(take["id"])["retired"])
        self.assertEqual(self.store.get_identity(self.identity["id"])["status"], "candidate")  # Never re-approved.

    def test_retiring_a_take_blocks_recordings_derived_from_it_until_restored(self):
        take = self.make_take()
        derived = self.store.add_reference_from_take(self.identity["id"], take["id"], transcript="Derived exact words.")
        self.store.retire("take", take["id"], reason="Bad take.")
        self.assertTrue(self.store.get_reference(derived["id"])["retired"])
        self.assertFalse(self.store.get_reference(self.reference["id"])["retired"])  # Its source recording is untouched.
        self.store.restore("take", take["id"])
        self.assertFalse(self.store.get_reference(derived["id"])["retired"])

    def test_restore_refuses_while_something_the_item_came_from_stays_retired(self):
        take = self.make_take()
        other = self.make_take(text="A second line here.")
        self.store.retire("reference", self.reference["id"])
        self.store.retire("take", take["id"])
        with self.assertRaisesRegex(ValueError, "stays retired"):
            self.store.restore("take", take["id"])
        self.assertIsNotNone(self.store.get_take(take["id"])["retirement"])  # The take keeps its own record.
        with self.assertRaisesRegex(ValueError, "came from"):
            self.store.restore("take", other["id"])

    def test_restore_of_an_item_that_is_not_retired_is_refused(self):
        take = self.make_take()
        with self.assertRaisesRegex(ValueError, "not retired"):
            self.store.restore("take", take["id"])

    def test_retiring_twice_keeps_a_single_active_record(self):
        take = self.make_take()
        self.store.retire("take", take["id"], reason="One.")
        second = self.store.retire("take", take["id"], reason="Two.")
        self.assertEqual(self.active_retirements(), 1)
        self.assertEqual(second["retirement"]["reason"], "One.")

    def test_retiring_a_character_blocks_new_voices_and_existing_voices(self):
        take = self.make_take()
        self.store.retire("character", self.character["id"], reason="Character withdrawn.")
        identity = self.store.get_identity(self.identity["id"])
        self.assertEqual(identity["retirement"]["source_kind"], "character")
        self.assertTrue(self.store.get_take(take["id"])["retired"])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.create_identity(self.character["id"], "qwen3-tts-1.7b-base", "New voice")
        self.store.restore("character", self.character["id"])
        self.assertFalse(self.store.get_take(take["id"])["retired"])

    def test_enforcement_points_refuse_new_use_of_a_retired_take(self):
        take = self.make_take()
        self.store.retire("take", take["id"])
        audio = self.store.get_media_path("take", take["id"])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.set_take_state(take["id"], status="selected")
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.add_reference_from_take(self.identity["id"], take["id"])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.assert_media_usable(audio)
        self.store.assert_media_usable(self.store.get_media_path("reference", self.reference["id"]))
        self.store.assert_identity_usable(self.identity["id"])

    def test_retired_recording_cannot_be_approved_or_used_as_a_reference_file(self):
        self.store.retire("reference", self.reference["id"])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.update_identity(self.identity["id"], {"status": "approved", "reference_version_id": self.reference["id"]})
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.assert_media_usable(self.store.get_media_path("reference", self.reference["id"]))

    def test_pickers_hide_retired_recordings_while_the_library_can_show_them(self):
        self.make_take()
        self.store.retire("reference", self.reference["id"])
        self.assertEqual(list_references(self.root, self.store), [])
        library = list_voice_library(self.root, self.store)
        self.assertEqual(library["voices"][0]["reference_count"], 0)
        self.assertIsNone(library["voices"][0]["audition"])
        shown = list_voice_library(self.root, self.store, include_retired=True)
        self.assertEqual(shown["voices"][0]["reference_count"], 1)
        self.assertIsNotNone(shown["voices"][0]["audition"])

    def test_export_refuses_a_retired_take(self):
        take = self.make_take()
        self.store.retire("take", take["id"])
        scene = {"scene_id": "scene-retired", "lines": [{
            "line_id": "line-01", "identity_id": self.identity["id"], "character_id": self.character["id"],
            "text": "Hello there, friend.", "use_pronunciations": False,
            "segments": [{"id": "seg-1", "text": "Hello there, friend."}],
            "selected_takes": {"seg-1": take["id"]}}]}
        with self.assertRaisesRegex(ExportError, "retired"):
            load_takes(self.root, scene)


class PurgeTests(CastFixture):
    def test_purge_requires_retirement_and_explicit_confirmation(self):
        take = self.make_take()
        with self.assertRaisesRegex(ValueError, "Retire"):
            self.store.purge("take", take["id"], confirm=True)
        self.store.retire("take", take["id"])
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.store.purge("take", take["id"])
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.store.purge("take", take["id"], confirm="yes")
        self.assertTrue(self.store.get_media_path("take", take["id"]).is_file())

    def test_purge_removes_audio_and_records_and_leaves_a_hashed_tombstone(self):
        take = self.make_take()
        audio = self.store.get_media_path("take", take["id"])
        folder = audio.parent
        digest = take["audio_sha256"]
        self.store.retire("take", take["id"])
        report = self.store.purge("take", take["id"], reason="Recorded by mistake.", confirm=True)
        self.assertEqual(report["purged"]["take"], 1)
        self.assertFalse(audio.exists())
        self.assertFalse(folder.exists())
        with self.assertRaises(KeyError):
            self.store.get_take(take["id"])
        db = sqlite3.connect(self.store.db_path)
        try:
            item = db.execute("SELECT batch_id,sha256s_json FROM purged_items WHERE target_kind='take' AND target_id=?",
                              (take["id"],)).fetchone()
            batch = db.execute("SELECT reason FROM purge_batches WHERE id=?", (item[0],)).fetchone()
        finally:
            db.close()
        self.assertEqual(json.loads(item[1]), [digest])
        self.assertEqual(batch[0], "Recorded by mistake.")
        self.assertTrue(self.store.take_summaries_for([take["id"]])[take["id"]]["purged"])

    def test_purging_a_recording_takes_its_takes_and_derived_versions_and_clears_the_approval(self):
        version = self.store.add_reference_version(self.reference["id"], {"transcript": "Edited exact transcript."})
        take = self.make_take()
        derived = self.store.add_reference_from_take(self.identity["id"], take["id"], transcript="Derived exact words.")
        files = [self.store.get_media_path("reference", ref_id) for ref_id in (self.reference["id"], version["id"], derived["id"])]
        files.append(self.store.get_media_path("take", take["id"]))
        self.store.retire("reference", self.reference["id"])
        preview = self.store.purge_preview("reference", self.reference["id"])
        self.assertEqual(preview["would_purge"], {"character": 0, "identity": 0, "reference": 3, "take": 1})
        report = self.store.purge("reference", self.reference["id"], confirm=True)
        self.assertEqual(report["purged"], preview["would_purge"])
        self.assertFalse(any(path.exists() for path in files))
        for ref_id in (self.reference["id"], version["id"], derived["id"]):
            with self.assertRaises(KeyError):
                self.store.get_reference(ref_id)
        with self.assertRaises(KeyError):
            self.store.get_take(take["id"])
        identity = self.store.get_identity(self.identity["id"])
        self.assertEqual((identity["status"], identity["approved_reference_id"]), ("candidate", None))

    def test_purging_a_voice_removes_its_records_and_keeps_other_voices(self):
        other_character = self.store.create_character("Tomas", "Quiet and precise")
        other_identity, other_reference = self.approved_voice(other_character["id"], "Tomas base", "tomas.wav")
        keep = self.make_take(identity=other_identity, reference=other_reference)
        self.make_take()
        self.store.retire("identity", self.identity["id"])
        self.store.purge("identity", self.identity["id"], confirm=True)
        remaining = {identity["id"] for character in self.store.list_cast() for identity in character["identities"]}
        self.assertEqual(remaining, {other_identity["id"]})
        self.assertTrue(self.store.get_media_path("take", keep["id"]).is_file())
        self.assertEqual(self.store.get_reference(other_reference["id"])["id"], other_reference["id"])

    def test_purging_a_character_removes_its_pronunciations_and_record(self):
        self.store.save_pronunciation(character_id=self.character["id"], source="Alduin", replacement="All doo in")
        self.store.retire("character", self.character["id"])
        self.store.purge("character", self.character["id"], confirm=True)
        self.assertEqual(self.store.list_cast(), [])
        db = sqlite3.connect(self.store.db_path)
        try:
            count = db.execute("SELECT COUNT(*) FROM pronunciations WHERE character_id=?", (self.character["id"],)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 0)

    def test_purge_stops_before_any_database_change_when_a_file_is_locked(self):
        take = self.make_take()
        audio = self.store.get_media_path("take", take["id"])
        self.store.retire("take", take["id"])
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("file is in use")):
            with self.assertRaisesRegex(ValueError, "Nothing was purged"):
                self.store.purge("take", take["id"], confirm=True)
        self.assertTrue(audio.is_file())
        self.assertEqual(self.store.get_take(take["id"])["id"], take["id"])

    def test_derived_copies_outside_cast_are_reported_not_deleted(self):
        take = self.make_take()
        copy = self.root / "out" / "exports" / "scene-copy.wav"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(self.store.get_media_path("take", take["id"]).read_bytes())
        self.store.retire("take", take["id"])
        self.assertEqual(self.store.purge_preview("take", take["id"])["external_copies"], ["out/exports/scene-copy.wav"])
        report = self.store.purge("take", take["id"], confirm=True)
        self.assertEqual(report["external_copies"], ["out/exports/scene-copy.wav"])
        self.assertTrue(copy.is_file())

    def test_batch_reads_report_retirement_and_purge_state(self):
        take = self.make_take()
        self.store.retire("take", take["id"], reason="Withdrawn.")
        self.assertEqual(self.store.take_summaries_for([take["id"]])[take["id"]]["retirement"]["reason"], "Withdrawn.")
        self.store.purge("take", take["id"], confirm=True)
        self.assertTrue(self.store.take_summaries_for([take["id"]])[take["id"]]["purged"])

    def test_migration_from_schema_four_adds_the_retirement_tables(self):
        db = sqlite3.connect(self.store.db_path)
        try:
            db.execute("DROP TABLE purged_items")
            db.execute("DROP TABLE purge_batches")
            db.execute("DROP TABLE retirements")
            db.execute("ALTER TABLE takes DROP COLUMN word_timings_json")  # schema four has no timing column
            db.execute("ALTER TABLE takes DROP COLUMN phoneme_timings_json")
            db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
            db.execute("DROP INDEX takes_parent")
            db.execute("ALTER TABLE takes DROP COLUMN edit_json")
            db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
            db.execute("PRAGMA user_version=4")
            db.commit()
        finally:
            db.close()
        reopened = CastStore(self.root)
        self.assertEqual(reopened.list_cast()[0]["id"], self.character["id"])
        db = sqlite3.connect(reopened.db_path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"retirements", "purge_batches", "purged_items"}.issubset(tables))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
        finally:
            db.close()


class GuardedPathTests(CastFixture):
    def test_raw_reference_paths_and_conversion_inputs_refuse_retired_recordings(self):
        take = self.make_take()
        reference_path = str(self.store.get_media_path("reference", self.reference["id"]))
        take_path = str(self.store.get_media_path("take", take["id"]))
        pool = EnginePool("cpu", 0, reference_roots=(self.store.data_root / "media" / "references",),
                          conversion_source_roots=(self.store.data_root / "media" / "takes",),
                          media_guard=self.store.assert_media_usable)
        try:
            self.assertEqual(Path(pool._resolve_reference_path("qwen3-tts-1.7b-base", reference_path)), Path(reference_path))
            self.assertEqual(serve._conversion_audio_path(take_path, "Source performance", target=False, cast=self.store)[0],
                             Path(take_path).resolve())
            self.store.retire("take", take["id"])
            self.store.retire("reference", self.reference["id"])
            with self.assertRaisesRegex(ValueError, "retired"):
                pool._resolve_reference_path("qwen3-tts-1.7b-base", reference_path)
            with self.assertRaisesRegex(ValueError, "retired"):
                serve._conversion_audio_path(take_path, "Source performance", target=False, cast=self.store)
        finally:
            pool.close()


class RetirementHttpTests(CastFixture):
    def setUp(self):
        super().setUp()
        self.engines = FakeEngines()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1], self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_retire_restore_and_purge_over_http(self):
        take = self.make_take()
        status, created = self.call("POST", "/api/retirements", {"kind": "take", "id": take["id"], "reason": "Recorded by mistake."})
        self.assertEqual(status, 201)
        self.assertEqual(created["retirement"]["reason"], "Recorded by mistake.")
        status, listed = self.call("GET", f"/api/takes?identity_id={self.identity['id']}&line_id=line-01")
        self.assertTrue(listed["takes"][0]["retired"])
        status, library = self.call("GET", "/api/voice-library")
        self.assertIsNone(library["voices"][0]["audition"])
        status, library = self.call("GET", "/api/voice-library?include_retired=1")
        self.assertEqual(library["voices"][0]["audition_count"], 1)
        status, preview = self.call("GET", f"/api/purges/preview?kind=take&id={take['id']}")
        self.assertEqual(preview["would_purge"]["take"], 1)
        status, refused = self.call("POST", "/api/purges", {"kind": "take", "id": take["id"]})
        self.assertEqual(status, 400)
        status, purged = self.call("POST", "/api/purges", {"kind": "take", "id": take["id"], "confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(purged["purged"]["take"], 1)
        status, missing = self.call("GET", f"/api/takes/{take['id']}")
        self.assertEqual(status, 404)

    def test_retired_voice_is_refused_by_generation_until_restored(self):
        self.call("POST", "/api/retirements", {"kind": "identity", "id": self.identity["id"], "reason": "Withdrawn."})
        body = {"identity_id": self.identity["id"], "line_id": "line-01", "text": "Hello there.",
                "language": "English", "settings": {}}
        status, refused = self.call("POST", "/api/performance", body)
        self.assertEqual(status, 400)
        self.assertIn("retired", refused["error"])
        status, restored = self.call("POST", "/api/retirements/restore", {"kind": "identity", "id": self.identity["id"]})
        self.assertEqual(status, 200)
        status, again = self.call("POST", "/api/retirements/restore", {"kind": "identity", "id": self.identity["id"]})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
