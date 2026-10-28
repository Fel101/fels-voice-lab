"""Loudness and peak readings: the numbers match signals whose answer is known, and every take carries them."""
import json
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import loudness  # noqa: E402
import serve  # noqa: E402
from cast_store import CAST_SCHEMA_VERSION, CastStore  # noqa: E402

RATE = 24_000


def sine(seconds=3.0, hz=997.0, amplitude=0.1, phase=0.0):
    time = np.arange(round(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * np.pi * hz * time + phase)).astype(np.float32)


class MeasureTests(unittest.TestCase):
    def test_a_known_tone_reads_as_the_standard_says(self):
        reading = loudness.measure(sine(), RATE)  # 997 Hz peaking at -20 dBFS is -23 LUFS
        self.assertEqual(reading["status"], "measured")
        self.assertAlmostEqual(reading["integrated_lufs"], -23.0, delta=0.2)
        self.assertAlmostEqual(reading["sample_peak_dbfs"], -20.0, delta=0.05)
        self.assertAlmostEqual(reading["true_peak_dbtp"], -20.0, delta=0.3)
        self.assertIsNone(reading["note"])

    def test_the_true_peak_finds_a_peak_that_falls_between_samples(self):
        quarter_rate = sine(seconds=1.0, hz=RATE / 4, amplitude=1.0, phase=math.pi / 4)  # every sample is 0.707 or less
        reading = loudness.measure(quarter_rate, RATE)
        self.assertAlmostEqual(reading["sample_peak_dbfs"], -3.01, delta=0.05)
        self.assertGreater(reading["true_peak_dbtp"], -0.5)
        self.assertGreater(reading["true_peak_dbtp"] - reading["sample_peak_dbfs"], 2.5)
        self.assertGreater(reading["true_peak_dbtp"], loudness.TRUE_PEAK_WARNING_DBTP)

    def test_audio_with_no_loudness_figure_says_why_and_keeps_the_peaks_it_has(self):
        silent = loudness.measure(np.zeros(RATE, dtype=np.float32), RATE)
        self.assertEqual((silent["note"], silent["integrated_lufs"], silent["sample_peak_dbfs"], silent["true_peak_dbtp"]),
                         ("silent", None, None, None))
        short = loudness.measure(sine(seconds=0.2), RATE)
        self.assertEqual((short["note"], short["integrated_lufs"]), ("too_short", None))
        self.assertAlmostEqual(short["sample_peak_dbfs"], -20.0, delta=0.05)
        self.assertIsNotNone(short["true_peak_dbtp"])
        quiet = loudness.measure(sine(seconds=2.0, amplitude=1e-6), RATE)
        self.assertEqual((quiet["note"], quiet["integrated_lufs"]), ("too_quiet", None))
        self.assertAlmostEqual(quiet["sample_peak_dbfs"], -120.0, delta=0.1)

    def test_audio_that_cannot_be_measured_is_refused(self):
        for bad in (np.array([], dtype=np.float32), np.array([0.1, np.nan], dtype=np.float32),
                    np.array([0.1, np.inf], dtype=np.float32)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                loudness.measure(bad, RATE)
        with self.assertRaises(ValueError):
            loudness.measure(sine(), 0)

    def test_two_channels_add_their_power(self):
        mono = loudness.measure(sine(), RATE)["integrated_lufs"]
        stereo = loudness.measure(np.stack([sine(), sine()], axis=1), RATE)["integrated_lufs"]
        self.assertAlmostEqual(stereo - mono, 3.0, delta=0.2)

    def test_a_saved_record_reads_back_and_a_missing_one_is_not_measured(self):
        self.assertEqual(loudness.stored(""), {"status": "not_measured", "meter": None})
        record = loudness.measure(sine(), RATE)
        self.assertEqual(loudness.stored(json.dumps(record)), record)
        unavailable = loudness.unavailable("x" * 500)
        self.assertEqual((unavailable["status"], len(unavailable["reason"])), ("unavailable", 300))

    def test_a_file_is_measured_from_the_samples_on_disk(self):
        import soundfile as sf
        with tempfile.TemporaryDirectory(prefix="voice-lab-loudness-") as folder:
            path = Path(folder) / "take.wav"
            sf.write(path, sine(), RATE, subtype="PCM_16")
            self.assertAlmostEqual(loudness.measure_file(path)["integrated_lufs"], -23.0, delta=0.2)


class TakeLoudnessStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-take-loudness-")
        self.addCleanup(self.temp.cleanup)
        self.store = CastStore(self.temp.name)
        character = self.store.create_character("Measured voice", "")
        identity = self.store.create_identity(character["id"], "kokoro", "Measured")
        self.generation = self.store.get_generation_identity(identity["id"])

    def save(self, samples, line_id="loudness"):
        return self.store.create_take(self.generation, line_id=line_id, text="Hello there.", spoken_text="Hello there.",
                                      language="English", direction="", settings={},
                                      model={"backend": "fixture", "checkpoint_revision": "fixture"},
                                      wav=samples, sample_rate=RATE, generation_info={})

    def column(self, take_id):
        db = sqlite3.connect(self.store.db_path)
        try:
            return db.execute("SELECT loudness_json FROM takes WHERE id=?", (take_id,)).fetchone()[0]
        finally:
            db.close()

    def forget_readings(self, *take_ids):
        db = sqlite3.connect(self.store.db_path)
        try:
            db.executemany("UPDATE takes SET loudness_json='' WHERE id=?", [(item,) for item in take_ids])
            db.commit()
        finally:
            db.close()

    def test_a_new_take_carries_its_readings_everywhere_it_is_listed(self):
        take = self.save(sine())
        self.assertEqual(take["loudness"]["status"], "measured")
        self.assertAlmostEqual(take["loudness"]["integrated_lufs"], -23.0, delta=0.2)
        self.assertEqual(json.loads(self.column(take["id"])), take["loudness"])
        self.assertEqual(self.store.get_take(take["id"])["loudness"], take["loudness"])
        self.assertEqual(self.store.list_takes(line_id="loudness")[0]["loudness"], take["loudness"])
        self.assertEqual(self.store.take_summaries_for([take["id"]])[take["id"]]["loudness"], take["loudness"])
        self.assertNotIn("loudness_json", take)

    def test_a_silent_take_is_saved_with_its_note_and_is_not_an_error(self):
        take = self.save(np.zeros(RATE, dtype=np.float32))
        self.assertEqual((take["loudness"]["status"], take["loudness"]["note"]), ("measured", "silent"))

    def test_measuring_trouble_never_discards_a_take(self):
        with mock.patch.object(loudness, "measure_file", side_effect=RuntimeError("meter broke")):
            take = self.save(sine())
        self.assertEqual((take["loudness"]["status"], take["loudness"]["reason"]), ("unavailable", "meter broke"))
        self.assertEqual(self.store.get_take(take["id"])["id"], take["id"])

    def test_older_takes_are_measured_once_and_only_once(self):
        first, second = self.save(sine(), "a"), self.save(sine(amplitude=0.2), "b")
        self.forget_readings(first["id"], second["id"])
        self.assertEqual(self.store.get_take(first["id"])["loudness"], {"status": "not_measured", "meter": None})
        self.assertEqual(self.store.measure_missing_loudness(), {"measured": 2, "failed": 0})
        self.assertAlmostEqual(self.store.get_take(first["id"])["loudness"]["integrated_lufs"], -23.0, delta=0.2)
        self.assertAlmostEqual(self.store.get_take(second["id"])["loudness"]["integrated_lufs"], -17.0, delta=0.2)
        with mock.patch.object(loudness, "measure_file", side_effect=AssertionError("measured again")):
            self.assertEqual(self.store.measure_missing_loudness(), {"measured": 0, "failed": 0})

    def test_a_take_whose_audio_is_gone_is_marked_unavailable_and_not_tried_again(self):
        take = self.save(sine())
        self.forget_readings(take["id"])
        self.store.get_media_path("take", take["id"]).unlink()
        self.assertEqual(self.store.measure_missing_loudness(), {"measured": 0, "failed": 1})
        record = self.store.get_take(take["id"])["loudness"]
        self.assertEqual(record["status"], "unavailable")
        self.assertIn("missing", record["reason"])
        self.assertEqual(self.store.measure_missing_loudness(), {"measured": 0, "failed": 0})

    def test_a_reading_saved_while_a_backfill_runs_is_not_overwritten(self):
        take = self.save(sine())
        self.forget_readings(take["id"])
        real = loudness.measure_file

        def measure_while_the_take_gets_its_own(path):
            db = sqlite3.connect(self.store.db_path)  # the take is saved with a record of its own just now
            try:
                db.execute("UPDATE takes SET loudness_json=? WHERE id=?", (json.dumps({"status": "measured", "mine": 1}), take["id"]))
                db.commit()
            finally:
                db.close()
            return real(path)

        with mock.patch.object(loudness, "measure_file", measure_while_the_take_gets_its_own):
            self.store.measure_missing_loudness()
        self.assertEqual(json.loads(self.column(take["id"])), {"status": "measured", "mine": 1})

    def test_a_schema_seven_database_gains_the_column_and_its_takes_read_as_not_measured(self):
        take = self.save(sine())
        db = sqlite3.connect(self.store.db_path)
        try:
            db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
            db.execute("DROP INDEX takes_parent")
            db.execute("ALTER TABLE takes DROP COLUMN edit_json")
            db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
            db.execute("PRAGMA user_version=7")
            db.commit()
        finally:
            db.close()
        reopened = CastStore(self.temp.name)
        self.assertEqual(reopened.get_take(take["id"])["loudness"], {"status": "not_measured", "meter": None})
        check = sqlite3.connect(reopened.db_path)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
        finally:
            check.close()
        self.assertEqual(reopened.measure_missing_loudness(), {"measured": 1, "failed": 0})

    def test_the_service_gives_older_takes_their_readings_at_start_and_says_so_in_its_log(self):
        take = self.save(sine())
        self.forget_readings(take["id"])
        events = []

        class Log:
            def event(self, message):
                events.append(message)

        serve.measure_old_takes(self.store, Log())
        self.assertEqual(events, ["loudness readings added to 1 older takes"])
        serve.measure_old_takes(self.store, Log())
        self.assertEqual(len(events), 1, "a second start has nothing to add and writes nothing")
        with mock.patch.object(self.store, "measure_missing_loudness", side_effect=RuntimeError("disk gone")):
            serve.measure_old_takes(self.store, Log())  # never raises
        self.assertIn("stopped: RuntimeError: disk gone", events[-1])


if __name__ == "__main__":
    unittest.main()
