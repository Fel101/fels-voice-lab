import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np
import soundfile as sf

from evaluation_diagnostics import analyze_audio, compare_transcript, take_checks
from evaluation_store import EvaluationStore, RATING_FIELDS, digest_json
from evaluation_suites import SUITES, get_suite
from library_fixture import use_old_comparison_folder


class EvaluationSuiteTests(unittest.TestCase):
    def test_named_suites_distinguish_common_controls_and_native_demos(self):
        ids = {suite["suite_id"] for suite in SUITES}
        self.assertTrue({"neutral-clone-three-line", "four-emotions-showcase", "native-events-showcase",
                         "short-barks-common", "names-numbers-common", "pronunciation-traps-common",
                         "multilingual-lines-common", "paragraph-continuity-common", "scene-consistency-common"}.issubset(ids))
        self.assertEqual(get_suite("neutral-clone-three-line")["repeat_count"], 3)
        self.assertEqual(get_suite("native-events-showcase")["scope"], "native_feature_demo")
        self.assertEqual(get_suite("four-emotions-showcase")["cases"][0]["scope"], "native_feature_demo")


class DiagnosticsTests(unittest.TestCase):
    def test_missing_words_truncation_and_repetition_are_flagged(self):
        evaluator = {"name": "fixture ASR", "version": "test-1"}
        truncated = compare_transcript("The red door is open now.", "The red door", evaluator)
        self.assertEqual(truncated["wer"], 3 / 6)
        self.assertIn("missing_words", truncated["flags"])
        self.assertIn("possible_truncation", truncated["flags"])
        repeated = compare_transcript("Go home now.", "Go home go home now.", evaluator)
        self.assertIn("repetition", repeated["flags"])
        self.assertIn("transcript_diff", repeated["flags"])

    def test_transcript_metadata_is_required_and_absent_transcript_stays_unknown(self):
        unknown = compare_transcript("Hello there.", None, None)
        self.assertEqual(unknown["status"], "not_transcribed")
        self.assertIsNone(unknown["wer"])
        with self.assertRaisesRegex(ValueError, "evaluator name and version"):
            compare_transcript("Hello there.", "Hello", {"name": "manual"})

    def test_silence_and_clipping_waveforms_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            silence = Path(tmp) / "silence.wav"
            clipping = Path(tmp) / "clip.wav"
            sf.write(silence, np.zeros(24_000, dtype=np.float32), 24_000, subtype="PCM_16")
            sf.write(clipping, np.ones(24_000, dtype=np.float32), 24_000, subtype="PCM_16")
            self.assertIn("silent_audio", analyze_audio(silence)["flags"])
            self.assertIn("clipping", analyze_audio(clipping)["flags"])

    def test_take_checks_flag_pauses_inside_the_take_but_not_leading_or_trailing_quiet(self):
        tone = 0.1 * np.sin(2 * np.pi * 220 * np.arange(12_000, dtype=np.float32) / 24_000)
        with tempfile.TemporaryDirectory() as tmp:
            inside, trailing = Path(tmp) / "inside.wav", Path(tmp) / "trailing.wav"
            sf.write(inside, np.concatenate([tone, np.zeros(36_000), tone]).astype(np.float32), 24_000, subtype="PCM_16")
            sf.write(trailing, np.concatenate([tone, np.zeros(48_000)]).astype(np.float32), 24_000, subtype="PCM_16")
            paused = take_checks(inside)
            self.assertEqual(paused["flags"], ["long_pause"])
            self.assertAlmostEqual(paused["longest_pause_seconds"], 1.5, places=3)
            self.assertEqual(take_checks(trailing)["flags"], [])


class EvaluationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lab = self.root / "voice-lab"
        self.out = self.lab / "out"
        self.out.mkdir(parents=True)
        self.db = self.lab / "data" / "evaluations.sqlite3"
        self.store = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)

    def tearDown(self):
        self.temp.cleanup()

    def _create_run(self):
        run_dir = self.out / "comparisons" / "fixture-run"
        raw = run_dir / "samples" / "raw"
        listening = run_dir / "samples" / "listening"
        raw.mkdir(parents=True)
        listening.mkdir(parents=True)
        samples = []
        for case_index, case_id in enumerate(("line-1", "line-2", "line-3")):
            for repetition in range(3):
                for engine_id, sample in (("engine-a", 0.1), ("engine-b", 0.2), ("engine-c", 0.3)):
                    name = f"{case_id}-{repetition}-{engine_id}.wav"
                    raw_path = raw / name
                    listening_path = listening / name
                    signal = np.full(24_000, sample + repetition * 0.001 + case_index * 0.002, dtype=np.float32)
                    sf.write(raw_path, signal, 24_000, subtype="PCM_16")
                    sf.write(listening_path, signal * 0.9, 24_000, subtype="PCM_16")
                    samples.append({"item_id": hashlib.md5(name.encode()).hexdigest(), "case_id": case_id,
                        "repetition": repetition, "engine_id": engine_id,
                        "expected_text": "The short test line is here.",
                        "raw_path": raw_path.relative_to(run_dir).as_posix(),
                        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                        "listening_path": listening_path.relative_to(run_dir).as_posix(),
                        "listening_sha256": hashlib.sha256(listening_path.read_bytes()).hexdigest(),
                        "metadata": {"test": True}, "diagnostics": {"flags": []}})
        manifest = {"schema_version": 1, "run_id": "fixture-run", "title": "Fixture run",
                    "suite_id": "fixture", "suite_version": 1, "created_at": "2026-09-29T00:00:00Z",
                    "summary": {}, "samples": samples}
        manifest["checksum"] = digest_json(manifest)
        path = run_dir / "results.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_manifest_validation_blinding_rating_and_restart(self):
        manifest_path = self._create_run()
        run = self.store.register_run_manifest(manifest_path)
        self.assertEqual(run["id"], "fixture-run")
        self.assertEqual(len(run["samples"]), 27)
        self.assertEqual(self.store.register_run_manifest(manifest_path)["manifest_sha256"], run["manifest_sha256"])
        session = self.store.create_blind_session("fixture-run", "test listener")
        self.assertEqual(session["state"], "blind")
        self.assertEqual(len(session["trials"]), 9)
        second_session = self.store.create_blind_session("fixture-run", "second listener")
        self.assertEqual(len(second_session["trials"]), 9)
        with self.store._connect() as db:
            rows = db.execute("SELECT s.id AS session_id,t.case_id,li.engine_id AS left_engine,ri.engine_id AS right_engine "
                "FROM blind_trials t JOIN blind_sessions s ON s.id=t.session_id "
                "JOIN items li ON li.id=t.left_item_id JOIN items ri ON ri.id=t.right_item_id "
                "WHERE s.run_id=? ORDER BY s.created_at,t.case_order", ("fixture-run",)).fetchall()
        by_session_pair = {}
        by_pair = {}
        for row in rows:
            pair = tuple(sorted((row["left_engine"], row["right_engine"])))
            key = (row["session_id"], pair)
            by_session_pair.setdefault(key, {}).setdefault(row["left_engine"], 0)
            by_session_pair[key][row["left_engine"]] += 1
            by_pair.setdefault(pair, {}).setdefault(row["left_engine"], 0)
            by_pair[pair][row["left_engine"]] += 1
        self.assertTrue(all(abs(counts.get(pair[0], 0) - counts.get(pair[1], 0)) <= 1
                            for (_session, pair), counts in by_session_pair.items()))
        self.assertTrue(all(abs(counts.get(pair[0], 0) - counts.get(pair[1], 0)) <= 1
                            for pair, counts in by_pair.items()))
        encoded = json.dumps(session)
        for engine_id in ("engine-a", "engine-b", "engine-c"):
            self.assertNotIn(engine_id, encoded)
        audio_url = session["trials"][0]["left"]["audio_url"]
        item_id = audio_url.rsplit("/", 1)[-1]
        self.assertTrue(self.store.get_blind_audio_path(item_id).is_file())
        scores = {field: 4 for field in RATING_FIELDS}
        with self.assertRaisesRegex(ValueError, "Rate every blind trial"):
            self.store.reveal_blind_session(session["session_id"])
        trial_id = session["trials"][0]["trial_id"]
        saved = self.store.save_rating(session["session_id"], trial_id, scores, "fixture only")
        self.assertEqual(saved["trials"][0]["rating"]["naturalness"], 4)
        self.assertEqual(saved["trials"][0]["rating_format"], "legacy-pair")
        self.assertFalse(saved["trials"][0]["rating_complete"])
        reopened = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        restored = reopened.get_blind_session(session["session_id"])
        self.assertEqual(restored["state"], "blind")
        self.assertEqual(restored["trials"][0]["rating"]["notes"], "fixture only")
        with self.assertRaisesRegex(ValueError, "both A and B"):
            reopened.reveal_blind_session(session["session_id"])
        side_scores = {"left": scores, "right": {field: 2 for field in RATING_FIELDS}, "preference": "A"}
        for trial in restored["trials"]:
            reopened.save_rating(session["session_id"], trial["trial_id"], side_scores)
        revealed = reopened.reveal_blind_session(session["session_id"])
        self.assertEqual(revealed["state"], "revealed")
        self.assertEqual(revealed["trials"][0]["rating"]["left"]["naturalness"], 4)
        self.assertEqual(revealed["trials"][0]["rating"]["right"]["naturalness"], 2)
        self.assertEqual(revealed["trials"][0]["legacy_rating"]["notes"], "fixture only")
        self.assertTrue(all("left_engine" in trial["revealed"] for trial in revealed["trials"]))
        with self.assertRaisesRegex(ValueError, "locked after"):
            reopened.save_rating(session["session_id"], trial_id, scores)

    def test_run_rejects_audio_path_escape_and_checksum_mismatch(self):
        manifest_path = self._create_run()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["samples"][0]["raw_path"] = "../../../../outside.wav"
        manifest.pop("checksum")
        manifest["checksum"] = digest_json(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escapes the run"):
            self.store.register_run_manifest(manifest_path)

    def test_schema_one_migration_preserves_legacy_rows_and_already_revealed_sessions(self):
        run = self.store.register_run_manifest(self._create_run())
        session = self.store.create_blind_session(run["id"], "Historical listener")
        trial_id = session["trials"][0]["trial_id"]
        self.store.save_rating(session["session_id"], trial_id, dict.fromkeys(RATING_FIELDS, 3),
                               "Original pair opinion, side unknown")
        with self.store._connect() as db:
            original = dict(db.execute("SELECT * FROM ratings WHERE trial_id=?", (trial_id,)).fetchone())
            db.execute("UPDATE blind_sessions SET state='revealed',revealed_at='historical-time' WHERE id=?",
                       (session["session_id"],))
            db.execute("DROP TABLE side_ratings")
            db.execute("PRAGMA user_version=1")
        reopened = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        with reopened._connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM ratings WHERE trial_id=?", (trial_id,)).fetchone()), original)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM side_ratings").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
        restored = reopened.get_blind_session(session["session_id"])
        self.assertEqual(restored["state"], "revealed")
        self.assertEqual(restored["revealed_at"], "historical-time")
        self.assertEqual(restored["trials"][0]["rating_format"], "legacy-pair")
        self.assertIsNotNone(restored["trials"][0]["legacy_rating"])
        self.assertFalse(restored["trials"][0]["rating_complete"])
        self.assertIn("revealed", restored["trials"][0])
        self.assertEqual(reopened.reveal_blind_session(session["session_id"]), restored)
        with self.assertRaisesRegex(ValueError, "locked after"):
            reopened.save_rating(session["session_id"], trial_id,
                {"left": dict.fromkeys(RATING_FIELDS, 5), "right": dict.fromkeys(RATING_FIELDS, 1)})

    def stored_paths(self, store):
        with store._connect() as db:
            return {row[0] for row in db.execute(
                "SELECT manifest_path FROM runs UNION SELECT raw_path FROM items UNION SELECT listening_path FROM items")}

    def schema_of(self, store):
        with store._connect() as db:
            return db.execute("PRAGMA user_version").fetchone()[0]

    def test_a_database_from_before_the_folder_was_renamed_has_the_folder_moved_and_its_paths_rewritten(self):
        run = self.store.register_run_manifest(self._create_run())
        use_old_comparison_folder(self.db, self.out)
        self.assertTrue(all(path.startswith("out/m5/") for path in self.stored_paths(self.store)))
        reopened = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        self.assertEqual(reopened.migration_notes, ["moved out/m5 to out/comparisons"])
        self.assertFalse((self.out / "m5").exists())
        self.assertTrue((self.out / "comparisons" / "fixture-run" / "results.json").is_file())
        paths = self.stored_paths(reopened)
        self.assertEqual(len(paths), 1 + 2 * len(run["samples"]))
        self.assertTrue(all(path.startswith("out/comparisons/fixture-run/") for path in paths))
        self.assertEqual(self.schema_of(reopened), 3)
        again = reopened.get_run(run["id"])
        self.assertEqual(again["manifest_sha256"], run["manifest_sha256"])
        self.assertTrue(all(reopened.get_blind_audio_path(sample["id"]).is_file() for sample in again["samples"]))
        copies = list((self.db.parent / "migration-backups").glob("evaluations-schema2-*.sqlite3"))
        self.assertEqual(len(copies), 1)
        with closing(sqlite3.connect(copies[0])) as old:
            self.assertTrue(old.execute("SELECT manifest_path FROM runs").fetchone()[0].startswith("out/m5/"))

    def test_a_move_that_stopped_after_the_folder_changed_name_is_finished_by_the_next_open(self):
        run = self.store.register_run_manifest(self._create_run())
        use_old_comparison_folder(self.db, self.out)
        (self.out / "m5").rename(self.out / "comparisons")
        reopened = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        self.assertEqual(reopened.migration_notes, [])
        self.assertTrue(all(path.startswith("out/comparisons/") for path in self.stored_paths(reopened)))
        self.assertEqual(self.schema_of(reopened), 3)
        self.assertTrue(reopened.get_blind_audio_path(run["samples"][0]["id"]).is_file())

    def test_a_folder_in_the_way_leaves_the_database_at_schema_two_until_it_is_cleared(self):
        run = self.store.register_run_manifest(self._create_run())
        use_old_comparison_folder(self.db, self.out)
        blocker = self.out / "comparisons" / "other-run"
        blocker.mkdir(parents=True)
        (blocker / "keep.txt").write_text("mine", "utf-8")
        blocked = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        self.assertEqual(len(blocked.migration_notes), 1)
        self.assertTrue(blocked.migration_notes[0].startswith("did not move out/m5 to out/comparisons: "))
        self.assertEqual(self.schema_of(blocked), 2)
        self.assertTrue(all(path.startswith("out/m5/") for path in self.stored_paths(blocked)))
        self.assertTrue((self.out / "m5" / "fixture-run" / "results.json").is_file())
        self.assertEqual((blocker / "keep.txt").read_text("utf-8"), "mine")
        (blocker / "keep.txt").rename(self.out / "keep.txt")
        blocker.rmdir()
        finished = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        self.assertEqual(finished.migration_notes, ["moved out/m5 to out/comparisons"])
        self.assertEqual(self.schema_of(finished), 3)
        self.assertTrue(finished.get_blind_audio_path(run["samples"][0]["id"]).is_file())

    def test_a_new_database_starts_at_the_current_schema_and_leaves_an_old_folder_alone(self):
        (self.out / "m5" / "stray").mkdir(parents=True)
        fresh = EvaluationStore(self.lab / "data" / "fresh.sqlite3", lab_root=self.lab, out_root=self.out)
        self.assertEqual(fresh.migration_notes, [])
        self.assertEqual(self.schema_of(fresh), 3)
        self.assertTrue((self.out / "m5" / "stray").is_dir())
        self.assertFalse((self.lab / "data" / "migration-backups").exists())

    def test_a_new_database_has_no_importer_tables_and_an_old_one_keeps_its_own(self):
        def tables(store):
            with store._connect() as db:
                return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({"legacy_sources", "legacy_records"} & tables(self.store))
        with self.store._connect() as db:
            db.execute("CREATE TABLE legacy_sources (source_sha256 TEXT PRIMARY KEY)")
            db.execute("INSERT INTO legacy_sources VALUES('kept')")
            db.execute("PRAGMA user_version=2")
        reopened = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        with reopened._connect() as db:
            self.assertEqual(db.execute("SELECT source_sha256 FROM legacy_sources").fetchone()[0], "kept")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_per_side_validation_atomic_save_updates_and_blind_reveal_gate(self):
        run = self.store.register_run_manifest(self._create_run())
        session = self.store.create_blind_session(run["id"])
        trial_id = session["trials"][0]["trial_id"]
        scores = dict.fromkeys(RATING_FIELDS, 4)
        invalid = [
            {"left": scores},
            {"left": {**scores, "artifacts": True}, "right": scores},
            {"left": scores, "right": {**scores, "naturalness": 0}},
            {"left": scores, "right": {key: value for key, value in scores.items() if key != "acting_fit"}},
            {"left": scores, "right": scores, "preference": "neither"},
            {"left": scores, "right": scores, "extra": "unsupported"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.save_rating(session["session_id"], trial_id, payload)
        self.assertFalse(self.store.get_blind_session(session["session_id"])["trials"][0]["rating_complete"])
        saved = self.store.save_rating(session["session_id"], trial_id,
            {"left": dict.fromkeys(RATING_FIELDS, 1), "right": dict.fromkeys(RATING_FIELDS, 5), "preference": "B"},
            "Synthetic per-side persistence fixture")
        trial = saved["trials"][0]
        self.assertEqual(trial["rating"]["left"]["naturalness"], 1)
        self.assertEqual(trial["rating"]["right"]["naturalness"], 5)
        self.assertEqual(trial["rating"]["preference"], "B")
        self.assertEqual(trial["rating_format"], "per-side")
        self.assertTrue(trial["rating_complete"])
        self.assertNotIn("revealed", trial)
        listing = next(row for row in self.store.list_blind_sessions() if row["id"] == session["session_id"])
        self.assertEqual(listing["rated_count"], 1)
        self.assertEqual(listing["legacy_rated_count"], 0)
        with self.assertRaisesRegex(ValueError, "both A and B"):
            self.store.reveal_blind_session(session["session_id"])
        self.store.save_rating(session["session_id"], trial_id,
            {"left": dict.fromkeys(RATING_FIELDS, 5), "right": dict.fromkeys(RATING_FIELDS, 2)}, "Revised fixture")
        reopened = EvaluationStore(self.db, lab_root=self.lab, out_root=self.out)
        revised = reopened.get_blind_session(session["session_id"])["trials"][0]["rating"]
        self.assertEqual(revised["left"]["naturalness"], 5)
        self.assertEqual(revised["right"]["naturalness"], 2)
        self.assertIsNone(revised["preference"])
        self.assertEqual(revised["notes"], "Revised fixture")

    def test_transcript_annotation_keeps_evaluator_and_word_scores(self):
        manifest_path = self._create_run()
        run = self.store.register_run_manifest(manifest_path)
        item_id = run["samples"][0]["id"]
        result = self.store.add_transcript(item_id, "The short line", {"name": "manual review", "version": "1"})
        self.assertIn("missing_words", result["diagnostics"]["flags"])
        item = self.store.get_item(item_id)
        self.assertEqual(item["transcript"]["evaluator"]["name"], "manual review")
        self.assertIsNotNone(item["transcript"]["cer"])


if __name__ == "__main__":
    unittest.main()
