"""Kokoro's phoneme times are kept with the take that made them, in their own column, and never in the engine record."""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402
from cast_store import CAST_SCHEMA_VERSION, CastStore  # noqa: E402
from worker_pool import read_catalog  # noqa: E402

TIMING = {"source": "kokoro-durations", "frame_seconds": 0.025, "unplaced_words": 0,
          "words": [{"word": "Hi", "start": 0.025, "end": 0.1}],
          "phonemes": [{"symbol": "h", "start": 0.025, "end": 0.05}, {"symbol": "I", "start": 0.05, "end": 0.1}]}
UNAVAILABLE = "the model gave no durations for a chunk of this clip"


def tone(seconds=1.0, sample_rate=24_000):
    time = np.arange(round(seconds * sample_rate), dtype=np.float32) / sample_rate
    return (0.1 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)


def raw_timing(db_path, take_id):
    db = sqlite3.connect(db_path)
    try:
        return db.execute("SELECT phoneme_timings_json FROM takes WHERE id=?", (take_id,)).fetchone()[0]
    finally:
        db.close()


class FakeKokoro:
    """Answers like the engine for Kokoro, adding whatever timing information the test sets."""

    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.timing_info = {}

    def speak(self, req):
        info = {"total_s": 0.1, "first_chunk_s": None, "language": "en-us", "voice": "af_heart",
                "phoneme_segments": []}
        info.update(self.timing_info)
        return tone(), 24_000, info


class KokoroTimingRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phoneme-route-")
        self.store = CastStore(self.temp.name)
        character = self.store.create_character("Kokoro reader", "")
        self.identity = self.store.create_identity(character["id"], "kokoro", "Kokoro af_heart")
        self.engines = FakeKokoro()
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

    def perform(self, text):
        body = json.dumps({"identity_id": self.identity["id"], "line_id": "line-01", "text": text,
                           "language": "English", "settings": {}}).encode("utf-8")
        request = urllib.request.Request(self.base + "/api/performance", data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.headers["X-Take-Id"]

    def test_kokoro_phoneme_times_are_stored_in_their_own_column_not_the_engine_record(self):
        self.engines.timing_info = {"timing": TIMING}
        take_id = self.perform("Hi.")
        take = self.store.get_take(take_id)
        self.assertTrue(take["has_phoneme_timings"])
        self.assertNotIn("timing", take["model"]["backend_result"])
        self.assertEqual(json.loads(raw_timing(self.store.db_path, take_id)), TIMING)

    def test_a_take_without_kokoro_timing_says_why_and_stores_none(self):
        self.engines.timing_info = {"timing_unavailable": UNAVAILABLE}
        take_id = self.perform("Hi.")
        take = self.store.get_take(take_id)
        self.assertFalse(take["has_phoneme_timings"])
        self.assertEqual(take["model"]["backend_result"]["timing_unavailable"], UNAVAILABLE)
        self.assertEqual(raw_timing(self.store.db_path, take_id), "")


class PhonemeTimingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phoneme-store-")
        self.store = CastStore(self.temp.name)
        character = self.store.create_character("Kokoro reader", "")
        identity = self.store.create_identity(character["id"], "kokoro", "Kokoro af_heart")
        self.generation = self.store.get_generation_identity(identity["id"])

    def tearDown(self):
        self.temp.cleanup()

    def save(self, timing=None):
        return self.store.create_take(self.generation, line_id="line-01", text="Hi.", spoken_text="Hi.",
                                      language="English", direction="", settings={},
                                      model={"backend": "fixture", "checkpoint_revision": "fixture"},
                                      wav=tone(), sample_rate=24_000, generation_info={}, phoneme_timing=timing)

    def test_a_take_keeps_its_phoneme_times_and_the_read_back_says_so(self):
        take = self.save(TIMING)
        self.assertTrue(take["has_phoneme_timings"])
        self.assertEqual(json.loads(raw_timing(self.store.db_path, take["id"])), TIMING)
        self.assertNotIn("phoneme_timings_json", take)

    def test_a_take_saved_without_phoneme_times_has_none(self):
        take = self.save()
        self.assertFalse(take["has_phoneme_timings"])
        self.assertEqual(raw_timing(self.store.db_path, take["id"]), "")


class PhonemeTimingMigrationTests(unittest.TestCase):
    def test_a_schema_six_database_gains_the_column_and_its_takes_have_no_phoneme_times(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-phoneme-migration-") as folder:
            store = CastStore(folder)
            character = store.create_character("Kokoro reader", "")
            identity = store.create_identity(character["id"], "kokoro", "Kokoro af_heart")
            take = store.create_take(store.get_generation_identity(identity["id"]), line_id="line-01", text="Hi.",
                                     spoken_text="Hi.", language="English", direction="", settings={},
                                     model={"backend": "fixture", "checkpoint_revision": "fixture"}, wav=tone(),
                                     sample_rate=24_000, generation_info={})
            db = sqlite3.connect(store.db_path)
            try:
                db.execute("ALTER TABLE takes DROP COLUMN phoneme_timings_json")
                db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
                db.execute("DROP INDEX takes_parent")
                db.execute("ALTER TABLE takes DROP COLUMN edit_json")
                db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
                db.execute("PRAGMA user_version=6")
                db.commit()
            finally:
                db.close()
            reopened = CastStore(folder)
            self.assertFalse(reopened.get_take(take["id"])["has_phoneme_timings"])
            db = sqlite3.connect(reopened.db_path)
            try:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
