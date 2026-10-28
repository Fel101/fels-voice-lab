"""Saving an edit of a take as a new take: what the new take is, how the parent's word and phoneme times move, what a repeated
save does, how retirement and purge reach edits, and the checks that keep an edit from starting on audio that is not the
take's own. The audio is synthetic; the stores are real, in temporary libraries."""
import hashlib
import io
import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio_fixtures import speech_between_silences
from cast_store import CAST_SCHEMA_VERSION, CastStore, RetirementReader
from library_fixture import temp_cast
from scene_store import SceneStore
from tools.scene_export import ExportError, load_takes
from worker_pool import read_catalog

RATE = 24_000
TEXT = "Hold the line now"
WORDS = (("Hold", 0.4, 0.7), ("the", 0.7, 0.9), ("line", 0.9, 1.3), ("now", 1.3, 1.6))
PHONEMES = (("h", 0.4, 0.5), ("O", 0.5, 0.7), ("D", 0.7, 0.8), ("e", 0.8, 0.9), ("l", 0.9, 1.0), ("I", 1.0, 1.2),
            ("n", 1.2, 1.3), ("n", 1.3, 1.4), ("a", 1.4, 1.5), ("W", 1.5, 1.6))
NEXT_SCHEMA_TAKE_COLUMNS = ("parent_take_id", "edit_json")


def word_record(audio_sha256):
    return {"schema": 1, "aligner": "fixture", "audio_sha256": audio_sha256, "language": "English", "text": TEXT,
            "aligned_at": "2026-10-26T00:00:00+00:00", "duration_seconds": 2.0, "checks": {"status": "ok"},
            "words": [{"word": word, "start": start, "end": end, "probability": 0.9} for word, start, end in WORDS]}


def phoneme_record():
    return {"frame_seconds": 0.0125, "unplaced_words": 0,
            "words": [{"word": word, "start": start, "end": end} for word, start, end in WORDS],
            "phonemes": [{"symbol": symbol, "start": start, "end": end} for symbol, start, end in PHONEMES]}


def tone_wav(seconds=1.0):
    result = io.BytesIO()
    sf.write(result, (0.1 * np.sin(2 * np.pi * 220 * np.arange(int(seconds * RATE)) / RATE)).astype(np.float32), RATE,
             format="WAV", subtype="PCM_16")
    return result.getvalue()


class EditedTakeCase(unittest.TestCase):
    """A Kokoro voice with one saved take: speech from 0.4 s to 1.6 s of 2.0 s, with word and phoneme times."""

    def setUp(self):
        self.store = temp_cast(self)
        self.character = self.store.create_character("Nell", "")
        self.identity = self.store.create_identity(self.character["id"], "kokoro", "Warm")
        self.parent = self.save_original()

    def save_original(self, *, line_id="line-01", direction="warm", request_key=None, timings=True, spoken_text=TEXT, rate=RATE):
        generation = self.store.get_generation_identity(self.identity["id"])
        take = self.store.create_take(
            generation, line_id=line_id, text=TEXT, spoken_text=spoken_text, language="English", direction=direction,
            settings={"speed": 1.0}, model={"backend": "fixture", "checkpoint_revision": "fixture"},
            wav=speech_between_silences(rate=rate), sample_rate=rate, generation_info={}, request_key=request_key,
            phoneme_timing=phoneme_record() if timings else None)
        if timings:
            self.store.set_word_timings(take["id"], word_record(take["audio_sha256"]))
        return self.store.get_take(take["id"])

    def edit(self, take=None, request=None, **more):
        """The edit saved as a new take (not the replayed flag)."""
        return self.store.create_edited_take((take or self.parent)["id"], request or {"start_seconds": 0.3, "end_seconds": 1.8},
                                             **more)[0]

    def samples(self, take):
        return sf.read(self.store.get_media_path("take", take["id"]), dtype="int16")[0]

    def stored_record(self, take, column):
        raw = self.store._row(f"SELECT {column} FROM takes WHERE id=?", (take["id"],))[0]
        return json.loads(raw) if raw else None

    def exportable_scene(self):
        take = self.save_original(line_id="scene-export:line-01", direction="")
        return take, {"scene_id": "scene-export", "lines": [{
            "line_id": "line-01", "identity_id": self.identity["id"], "character_id": self.character["id"], "engine_id": "kokoro",
            "reference_version_id": None, "text": TEXT, "direction": "", "language": "English", "settings": {},
            "use_pronunciations": False, "segments": [{"id": "seg-1", "text": TEXT}], "selected_takes": {"seg-1": take["id"]}}]}

    def take_folders(self):
        folder = self.store.data_root / "media" / "takes"
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


class SavingTests(EditedTakeCase):
    def test_an_edit_is_a_new_candidate_take_that_names_its_parent(self):
        take, replayed = self.store.create_edited_take(self.parent["id"], {"start_seconds": 0.3, "end_seconds": 1.8})
        self.assertFalse(replayed)
        self.assertNotEqual(take["id"], self.parent["id"])
        self.assertEqual((take["parent_take_id"], take["status"], take["rating"], take["notes"], take["retired"]),
                         (self.parent["id"], "candidate", None, "", False))
        for field in ("character_id", "character_name", "identity_id", "identity_label", "engine_id", "reference_version_id",
                      "line_id", "text", "spoken_text", "language", "direction", "settings", "model", "sample_rate"):
            self.assertEqual(take[field], self.parent[field], field)
        self.assertAlmostEqual(take["duration_seconds"], 1.5, places=6)
        self.assertEqual(take["edit"]["request"], {
            "start_seconds": 0.3, "end_seconds": 1.8, "auto_trim": False, "silence_threshold_db": -50.0,
            "keep_silence_ms": 80.0, "fade_in_ms": 0.0, "fade_out_ms": 0.0, "gain_db": 0.0})
        self.assertEqual((take["edit"]["result"]["start_seconds"], take["edit"]["result"]["end_seconds"]), (0.3, 1.8))
        self.assertEqual((take["edit"]["parent_audio_sha256"], take["edit"]["parent_duration_seconds"]),
                         (self.parent["audio_sha256"], 2.0))
        self.assertEqual(self.store.get_take(take["id"]), take)

    def test_an_original_names_no_parent_and_has_no_edit(self):
        self.assertIsNone(self.parent["parent_take_id"])
        self.assertIsNone(self.parent["edit"])

    def test_the_text_and_the_text_sent_to_the_voice_are_both_kept(self):
        parent = self.save_original(line_id="line-03", spoken_text="Hold the line, now.")
        take = self.edit(parent)
        self.assertEqual((take["text"], take["spoken_text"]), (TEXT, "Hold the line, now."))

    def test_what_was_decided_about_the_parent_is_not_inherited(self):
        self.store.set_take_state(self.parent["id"], status="selected", rating=5, notes="the one")
        take = self.edit()
        self.assertEqual((take["status"], take["rating"], take["notes"]), ("candidate", None, ""))
        self.assertEqual(self.store.get_take(self.parent["id"])["status"], "selected")       # saving an edit leaves the original as it was

    def test_an_edit_is_saved_as_16_bit_audio_at_the_takes_own_rate(self):
        parent = self.save_original(line_id="line-22k", rate=22_050)
        take = self.edit(parent, {"start_seconds": 0.3, "end_seconds": 1.8, "gain_db": -3})
        info = sf.info(str(self.store.get_media_path("take", take["id"])))
        self.assertEqual((info.samplerate, info.subtype, take["sample_rate"]), (22_050, "PCM_16", 22_050))
        self.assertEqual(info.frames, round(1.8 * 22_050) - round(0.3 * 22_050))
        self.assertEqual(self.store.take_waveform(parent["id"], 100)["sample_rate"], 22_050)
        audio, _report = self.store.preview_edit(parent["id"], {"gain_db": -3})
        self.assertEqual(sf.info(io.BytesIO(audio)).samplerate, 22_050)

    def test_an_edit_is_checked_against_the_takes_own_length(self):
        for request in ({"end_seconds": 2.5}, {"start_seconds": 2.5, "end_seconds": 3.0}):          # the take is 2.0 s long
            with self.subTest(request):
                with self.assertRaisesRegex(ValueError, "must be from 0 to 2"):
                    self.store.preview_edit(self.parent["id"], request)
                with self.assertRaisesRegex(ValueError, "must be from 0 to 2"):
                    self.store.create_edited_take(self.parent["id"], request)

    def test_a_trim_alone_copies_the_samples_exactly(self):
        take = self.edit(request={"start_seconds": 0.3, "end_seconds": 1.8})
        self.assertTrue(np.array_equal(self.samples(take), self.samples(self.parent)[7200:43200]))
        stored = self.store.get_media_path("take", take["id"]).read_bytes()
        self.assertEqual(take["audio_sha256"], hashlib.sha256(stored).hexdigest())
        self.assertEqual((take["sample_rate"], len(self.samples(take))), (RATE, 36000))

    def test_the_parent_is_never_changed(self):
        audio = self.store.get_media_path("take", self.parent["id"]).read_bytes()
        record = self.store.get_take(self.parent["id"])
        stored = (self.stored_record(self.parent, "word_timings_json"), self.stored_record(self.parent, "phoneme_timings_json"))
        self.edit(request={"start_seconds": 0.5, "gain_db": -6, "fade_in_ms": 20})
        self.assertEqual(self.store.get_media_path("take", self.parent["id"]).read_bytes(), audio)
        self.assertEqual(self.store.get_take(self.parent["id"]), record)
        self.assertEqual((self.stored_record(self.parent, "word_timings_json"),
                          self.stored_record(self.parent, "phoneme_timings_json")), stored)

    def test_provenance_keeps_the_parents_and_names_the_edit(self):
        take = self.edit()
        self.assertEqual(take["provenance"]["edited_from_take_id"], self.parent["id"])
        self.assertEqual(take["provenance"]["edit_request"], take["edit"]["request"])
        for key, value in self.parent["provenance"].items():
            self.assertEqual(take["provenance"][key], value, key)
        self.assertNotIn("edited_from_take_id", self.parent["provenance"])

    def test_loudness_and_checks_are_measured_again_on_the_new_audio(self):
        louder = self.edit(request={"gain_db": 6})
        self.assertEqual(louder["loudness"]["status"], "measured")
        self.assertAlmostEqual(louder["loudness"]["sample_peak_dbfs"] - self.parent["loudness"]["sample_peak_dbfs"], 6.0,
                               delta=0.05)
        short = self.edit(request={"start_seconds": 0.8, "end_seconds": 1.0})
        self.assertNotIn("duration_suspiciously_short", self.parent["waveform_checks"]["flags"])
        self.assertIn("duration_suspiciously_short", short["waveform_checks"]["flags"])

    def test_an_edit_of_an_edit_names_the_edit_it_started_from(self):
        first = self.edit(request={"start_seconds": 0.3, "end_seconds": 1.8})
        second = self.edit(first, {"start_seconds": 0.2, "end_seconds": 1.2})
        self.assertEqual(second["parent_take_id"], first["id"])
        self.assertEqual(second["provenance"]["edited_from_take_id"], first["id"])
        self.assertTrue(np.array_equal(self.samples(second), self.samples(self.parent)[12000:36000]))

    def test_an_edit_is_listed_with_the_takes_of_its_line(self):
        take = self.edit()
        listed = self.store.list_takes(identity_id=self.identity["id"], line_id="line-01")
        self.assertEqual([item["id"] for item in listed], [self.parent["id"], take["id"]])
        self.assertEqual([item["parent_take_id"] for item in listed], [None, self.parent["id"]])

    def test_selecting_an_edit_returns_the_original_to_candidate(self):
        self.store.set_take_state(self.parent["id"], status="selected")
        take = self.edit()
        self.store.set_take_state(take["id"], status="selected")
        self.assertEqual(self.store.get_take(self.parent["id"])["status"], "candidate")
        self.assertEqual(self.store.get_take(take["id"])["status"], "selected")

    def test_a_preview_saves_nothing_and_is_what_saving_makes(self):
        request = {"start_seconds": 0.3, "end_seconds": 1.8, "fade_in_ms": 25, "gain_db": 3}
        before = (self.store.all_take_ids(), self.take_folders())
        audio, report = self.store.preview_edit(self.parent["id"], request)
        self.assertEqual((self.store.all_take_ids(), self.take_folders()), before)
        previewed = sf.read(io.BytesIO(audio), dtype="int16")[0]
        saved = self.edit(request=request)
        self.assertTrue(np.array_equal(previewed, self.samples(saved)))
        self.assertEqual(report, {key: saved["edit"]["result"][key] for key in report})

    def test_a_refused_edit_leaves_nothing_behind(self):
        before = (self.store.all_take_ids(), self.take_folders())
        with self.assertRaisesRegex(ValueError, "would clip"):
            self.store.create_edited_take(self.parent["id"], {"gain_db": 24})
        with self.assertRaisesRegex(ValueError, "changes nothing"):
            self.store.create_edited_take(self.parent["id"], {})
        self.assertEqual((self.store.all_take_ids(), self.take_folders()), before)

    def test_a_failure_while_saving_removes_the_new_audio(self):
        before = (self.store.all_take_ids(), self.take_folders())
        with mock.patch("cast_store._waveform_checks", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.store.create_edited_take(self.parent["id"], {"start_seconds": 0.3})
        self.assertEqual((self.store.all_take_ids(), self.take_folders()), before)

    def test_a_take_that_does_not_exist_is_not_found(self):
        with self.assertRaises(KeyError):
            self.store.create_edited_take("0" * 32, {"gain_db": 3})
        with self.assertRaises(KeyError):
            self.store.preview_edit("0" * 32, {"gain_db": 3})
        with self.assertRaisesRegex(ValueError, "take id"):
            self.store.create_edited_take("not-an-id", {"gain_db": 3})


class TimingTests(EditedTakeCase):
    def test_word_times_move_with_the_trim(self):
        take = self.edit(request={"start_seconds": 0.5, "end_seconds": 1.5})
        record = self.stored_record(take, "word_timings_json")
        self.assertEqual([(item["word"], item["start"], item["end"]) for item in record["words"]],
                         [("Hold", 0.0, 0.2), ("the", 0.2, 0.4), ("line", 0.4, 0.8), ("now", 0.8, 1.0)])
        self.assertEqual([item["probability"] for item in record["words"]], [0.9] * 4)
        self.assertEqual((record["audio_sha256"], record["duration_seconds"], record["text"], record["aligner"]),
                         (take["audio_sha256"], 1.0, TEXT, "fixture"))
        self.assertEqual(record["derived_from"], {"take_id": self.parent["id"], "audio_sha256": self.parent["audio_sha256"],
                                                  "start_seconds": 0.5, "end_seconds": 1.5, "spans_dropped": 0,
                                                  "spans_shortened": 2})
        self.assertTrue(take["has_word_timings"])
        self.assertEqual(take["edit"]["word_times"], "moved")

    def test_a_word_the_trim_removes_leaves_the_record_and_the_text_follows(self):
        take = self.edit(request={"start_seconds": 0.7, "end_seconds": 1.3})
        record = self.stored_record(take, "word_timings_json")
        self.assertEqual([item["word"] for item in record["words"]], ["the", "line"])
        self.assertEqual(record["text"], "the line")
        self.assertEqual(record["derived_from"]["spans_dropped"], 2)

    def test_phoneme_times_move_with_the_trim(self):
        take = self.edit(request={"start_seconds": 0.5, "end_seconds": 1.5})
        record = self.stored_record(take, "phoneme_timings_json")
        self.assertEqual([(item["symbol"], item["start"], item["end"]) for item in record["phonemes"]],
                         [("O", 0.0, 0.2), ("D", 0.2, 0.3), ("e", 0.3, 0.4), ("l", 0.4, 0.5), ("I", 0.5, 0.7),
                          ("n", 0.7, 0.8), ("n", 0.8, 0.9), ("a", 0.9, 1.0)])
        self.assertEqual((record["audio_sha256"], record["duration_seconds"], record["frame_seconds"]),
                         (take["audio_sha256"], 1.0, 0.0125))
        self.assertEqual(record["derived_from"]["take_id"], self.parent["id"])
        self.assertTrue(take["has_phoneme_timings"])
        self.assertEqual(take["edit"]["phoneme_times"], "moved")

    def test_word_times_follow_the_cut_an_auto_trim_made_not_the_one_asked_for(self):
        # the speech starts at 0.4 s; the first 20 ms window to reach it starts at 0.39 s, and 100 ms is kept before that
        take = self.edit(request={"auto_trim": True, "keep_silence_ms": 100})
        self.assertEqual(take["edit"]["result"]["start_seconds"], 0.29)
        record = self.stored_record(take, "word_timings_json")
        self.assertEqual((record["words"][0]["word"], record["words"][0]["start"]), ("Hold", 0.11))
        self.assertEqual(record["derived_from"]["start_seconds"], 0.29)

    def test_a_parent_with_no_times_gives_an_edit_with_none(self):
        bare = self.save_original(line_id="line-02", timings=False)
        take = self.edit(bare)
        self.assertEqual((take["has_word_timings"], take["has_phoneme_timings"]), (False, False))
        self.assertEqual((take["edit"]["word_times"], take["edit"]["phoneme_times"]), ("none", "none"))
        self.assertIn(take["id"], self.store.take_ids_without_word_timings())

    def test_a_failed_alignment_is_not_carried_to_the_edit(self):
        self.store.set_word_timings(self.parent["id"], {"schema": 1, "error": "no result"})
        take = self.edit()
        self.assertFalse(take["has_word_timings"])
        self.assertEqual(take["edit"]["word_times"], "none")
        self.assertIn(take["id"], self.store.take_ids_without_word_timings())
        self.assertNotIn(self.parent["id"], self.store.take_ids_without_word_timings())

    def test_fades_and_gain_leave_the_times_where_they_were(self):
        take = self.edit(request={"gain_db": -3, "fade_out_ms": 50})
        record = self.stored_record(take, "word_timings_json")
        self.assertEqual([(item["start"], item["end"]) for item in record["words"]], [(start, end) for _w, start, end in WORDS])
        self.assertEqual(record["derived_from"]["spans_dropped"], 0)
        self.assertEqual(record["audio_sha256"], take["audio_sha256"])
        self.assertNotEqual(record["audio_sha256"], self.parent["audio_sha256"])

    def test_the_times_of_an_edit_of_an_edit_follow_both_trims(self):
        first = self.edit(request={"start_seconds": 0.3, "end_seconds": 1.8})
        second = self.edit(first, {"start_seconds": 0.2, "end_seconds": 1.2})
        record = self.stored_record(second, "word_timings_json")
        self.assertEqual([(item["word"], item["start"], item["end"]) for item in record["words"]],
                         [("Hold", 0.0, 0.2), ("the", 0.2, 0.4), ("line", 0.4, 0.8), ("now", 0.8, 1.0)])
        self.assertEqual(record["derived_from"]["take_id"], first["id"])


class RepeatedSaveTests(EditedTakeCase):
    REQUEST = {"start_seconds": 0.3, "end_seconds": 1.8, "gain_db": 2}

    def test_the_same_key_and_edit_return_the_take_that_was_made(self):
        first, replayed = self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="edit-1")
        self.assertFalse(replayed)
        again, replayed = self.store.create_edited_take(self.parent["id"], dict(self.REQUEST), request_key="edit-1")
        self.assertTrue(replayed)
        self.assertEqual(again, first)
        spelled = {**self.REQUEST, "start_seconds": 0.30000001, "auto_trim": False, "fade_in_ms": None}
        self.assertEqual(self.store.create_edited_take(self.parent["id"], spelled, request_key="edit-1"), (first, True))
        self.assertEqual(len(self.store.all_take_ids()), 2)
        self.assertEqual(len(self.take_folders()), 2)

    def test_a_repeat_is_answered_without_rendering_anything(self):
        first, _ = self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="edit-1")
        with mock.patch.object(self.store, "_render_edit", side_effect=AssertionError("a repeat renders nothing")):
            self.assertEqual(self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="edit-1"), (first, True))

    def test_a_key_used_for_a_different_edit_or_another_take_is_refused(self):
        self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="edit-1")
        with self.assertRaisesRegex(ValueError, "already used with different inputs"):
            self.store.create_edited_take(self.parent["id"], {**self.REQUEST, "gain_db": 3}, request_key="edit-1")
        other = self.save_original(line_id="line-02")
        with self.assertRaisesRegex(ValueError, "already used with different inputs"):
            self.store.create_edited_take(other["id"], self.REQUEST, request_key="edit-1")
        self.assertEqual(len(self.store.all_take_ids()), 3)

    def test_a_key_a_performance_used_is_refused_for_an_edit(self):
        self.save_original(line_id="line-02", request_key="performance-1")
        with self.assertRaisesRegex(ValueError, "already used with different inputs"):
            self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="performance-1")

    def test_a_key_an_edit_used_is_not_a_performances_replay(self):
        take, _ = self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="edit-1")
        generation = self.store.get_generation_identity(self.identity["id"])
        with self.assertRaisesRegex(ValueError, "already used with different inputs"):
            self.store.create_take(generation, line_id="line-01", text=TEXT, spoken_text=TEXT, language="English",
                                   direction="warm", settings={"speed": 1.0},
                                   model={"backend": "fixture", "checkpoint_revision": "fixture"}, wav=speech_between_silences(),
                                   sample_rate=RATE, generation_info={}, request_key="edit-1")
        self.assertEqual(self.store.get_take_by_request_key("edit-1")["id"], take["id"])

    def test_a_key_is_text_of_one_to_two_hundred_characters(self):
        for key in ("", "x" * 201, 5, ["a"]):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "idempotency key must contain"):
                self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key=key)
        self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="x" * 200)

    def test_two_saves_at_once_with_one_key_make_one_take(self):
        barrier = threading.Barrier(2, timeout=10)
        render = self.store._render_edit

        def meet_then_render(parent, edit):
            barrier.wait()
            return render(parent, edit)

        results, errors = [], []

        def save():
            try:
                results.append(self.store.create_edited_take(self.parent["id"], self.REQUEST, request_key="race"))
            except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                errors.append(exc)

        with mock.patch.object(self.store, "_render_edit", meet_then_render):
            threads = [threading.Thread(target=save) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(replayed for _take, replayed in results), [False, True])
        self.assertEqual(results[0][0]["id"], results[1][0]["id"])
        self.assertEqual(len(self.store.all_take_ids()), 2)
        self.assertEqual(len(self.take_folders()), 2, "the save that lost the race must remove its audio")

    def test_two_saves_at_once_with_one_key_and_different_edits_make_one_take_and_refuse_the_other(self):
        barrier = threading.Barrier(2, timeout=10)
        render = self.store._render_edit

        def meet_then_render(parent, edit):
            barrier.wait()
            return render(parent, edit)

        outcomes = []

        def save(request):
            try:
                outcomes.append(("saved", self.store.create_edited_take(self.parent["id"], request, request_key="race")))
            except ValueError as exc:
                outcomes.append(("refused", str(exc)))

        with mock.patch.object(self.store, "_render_edit", meet_then_render):
            threads = [threading.Thread(target=save, args=({"gain_db": gain},)) for gain in (2, 3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["refused", "saved"])
        self.assertIn("already used with different inputs", next(value for kind, value in outcomes if kind == "refused"))
        self.assertEqual(len(self.store.all_take_ids()), 2)
        self.assertEqual(len(self.take_folders()), 2, "the save that was refused must remove its audio")


class RetirementAndPurgeTests(EditedTakeCase):
    def test_a_retired_take_cannot_be_edited_or_previewed(self):
        self.store.retire("take", self.parent["id"])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.create_edited_take(self.parent["id"], {"gain_db": 3})
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.preview_edit(self.parent["id"], {"gain_db": 3})

    def test_retiring_a_take_retires_its_edits_and_restoring_lifts_it(self):
        child = self.edit()
        grandchild = self.edit(child, {"gain_db": 2})
        self.store.retire("take", self.parent["id"], reason="Withdrawn.")
        for take in (child, grandchild):
            state = self.store.get_take(take["id"])
            self.assertTrue(state["retired"])
            self.assertEqual((state["retirement"]["inherited"], state["retirement"]["source_id"]), (True, self.parent["id"]))
            with self.assertRaisesRegex(ValueError, "retired"):
                self.store.create_edited_take(take["id"], {"gain_db": 1})
        self.store.restore("take", self.parent["id"])
        self.assertFalse(self.store.get_take(child["id"])["retired"])
        self.assertFalse(self.store.get_take(grandchild["id"])["retired"])

    def test_retiring_an_edit_leaves_its_parent_alone(self):
        child = self.edit()
        self.store.retire("take", child["id"])
        self.assertTrue(self.store.get_take(child["id"])["retired"])
        self.assertFalse(self.store.get_take(self.parent["id"])["retired"])
        self.edit()  # the parent still takes edits

    def test_purging_a_take_deletes_its_edits_and_their_audio(self):
        child = self.edit()
        grandchild = self.edit(child, {"gain_db": 2})
        folders = [self.store.get_media_path("take", item["id"]).parent for item in (self.parent, child, grandchild)]
        self.store.retire("take", self.parent["id"])
        preview = self.store.purge_preview("take", self.parent["id"])
        self.assertEqual(preview["would_purge"], {"character": 0, "identity": 0, "reference": 0, "take": 3})
        report = self.store.purge("take", self.parent["id"], confirm=True)
        self.assertEqual(report["purged"], preview["would_purge"])
        self.assertFalse(any(folder.exists() for folder in folders))
        for item in (self.parent, child, grandchild):
            with self.assertRaises(KeyError):
                self.store.get_take(item["id"])
        self.assertEqual(self.store.all_take_ids(), [])

    def test_purging_an_edit_keeps_its_parent(self):
        child = self.edit()
        audio = self.store.get_media_path("take", self.parent["id"])
        self.store.retire("take", child["id"])
        self.assertEqual(self.store.purge("take", child["id"], confirm=True)["purged"]["take"], 1)
        self.assertTrue(audio.is_file())
        self.assertEqual(self.store.get_take(self.parent["id"])["id"], self.parent["id"])
        with self.assertRaises(KeyError):
            self.store.get_take(child["id"])

    def test_purging_a_voice_takes_its_edits_with_it(self):
        self.edit()
        self.store.retire("identity", self.identity["id"])
        self.store.purge("identity", self.identity["id"], confirm=True)
        self.assertEqual((self.store.all_take_ids(), self.take_folders()), ([], []))


class IntegrityTests(EditedTakeCase):
    def stored_file(self):
        return self.store.get_media_path("take", self.parent["id"])

    def refused(self, pattern):
        before = (self.store.all_take_ids(), self.take_folders())
        for action in (lambda: self.store.create_edited_take(self.parent["id"], {"gain_db": 3}),
                       lambda: self.store.preview_edit(self.parent["id"], {"gain_db": 3}),
                       lambda: self.store.take_waveform(self.parent["id"])):
            with self.assertRaisesRegex(ValueError, pattern):
                action()
        self.assertEqual((self.store.all_take_ids(), self.take_folders()), before)

    def test_audio_that_no_longer_matches_the_checksum_is_refused(self):
        sf.write(self.stored_file(), speech_between_silences()[:RATE], RATE, format="WAV", subtype="PCM_16")
        self.refused("checksum")

    def test_audio_that_is_missing_is_refused(self):
        self.stored_file().unlink()
        self.refused("missing")

    def test_the_waveform_checks_how_many_points_are_asked_for(self):
        self.assertEqual(self.store.take_waveform(self.parent["id"], 600)["points"], 600)
        self.assertEqual(self.store.take_waveform(self.parent["id"], "10")["points"], 10)
        self.assertEqual(self.store.take_waveform(self.parent["id"], 2000)["points"], 2000)
        for points in (9, 2001, "9", "2001", "abc", "-5", "1.5", "", True, None, 12.5, "²", "١٠"):
            with self.subTest(points=points), self.assertRaisesRegex(ValueError, "points must be a whole number from 10 to 2000"):
                self.store.take_waveform(self.parent["id"], points)

    def test_the_waveform_describes_the_take(self):
        wave = self.store.take_waveform(self.parent["id"], 100)
        self.assertEqual((wave["duration_seconds"], wave["sample_rate"], len(wave["peaks"])), (2.0, RATE, 100))
        self.assertAlmostEqual(wave["speech"]["start_seconds"], 0.4, delta=0.03)
        self.assertAlmostEqual(wave["speech"]["end_seconds"], 1.6, delta=0.03)
        self.assertAlmostEqual(wave["peak_dbfs"], -10.46, delta=0.05)


class OlderLibraryTests(EditedTakeCase):
    def downgrade_to_schema_eight(self):
        db = sqlite3.connect(self.store.db_path)
        try:
            db.execute("DROP INDEX takes_parent")
            for column in NEXT_SCHEMA_TAKE_COLUMNS:
                db.execute(f"ALTER TABLE takes DROP COLUMN {column}")
            db.execute("PRAGMA user_version=8")
            db.commit()
        finally:
            db.close()

    def test_a_new_library_has_the_edit_columns_and_the_parent_index(self):
        db = sqlite3.connect(self.store.db_path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(takes)")}
            indexes = {row[1] for row in db.execute("PRAGMA index_list(takes)")}
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
        finally:
            db.close()
        self.assertTrue(set(NEXT_SCHEMA_TAKE_COLUMNS) <= columns)
        self.assertIn("takes_parent", indexes)
        self.assertIn("WHERE parent_take_id IS NOT NULL", self.parent_index_sql(self.store.db_path), "originals are not indexed")

    @staticmethod
    def parent_index_sql(db_path):
        db = sqlite3.connect(db_path)
        try:
            return db.execute("SELECT sql FROM sqlite_master WHERE name='takes_parent'").fetchone()[0]
        finally:
            db.close()

    def test_a_schema_eight_library_gains_the_columns_and_its_takes_are_originals(self):
        self.downgrade_to_schema_eight()
        reopened = CastStore(self.store.root)
        take = reopened.get_take(self.parent["id"])
        self.assertEqual((take["parent_take_id"], take["edit"]), (None, None))
        db = sqlite3.connect(reopened.db_path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], CAST_SCHEMA_VERSION)
            self.assertIn("takes_parent", {row[1] for row in db.execute("PRAGMA index_list(takes)")})
        finally:
            db.close()
        self.assertIn("WHERE parent_take_id IS NOT NULL", self.parent_index_sql(reopened.db_path))
        edited, _ = reopened.create_edited_take(self.parent["id"], {"start_seconds": 0.3})
        self.assertEqual(reopened.get_take(edited["id"])["parent_take_id"], self.parent["id"])

    def test_export_reads_a_schema_eight_library_the_lab_has_not_migrated(self):
        take, scene = self.exportable_scene()
        self.downgrade_to_schema_eight()
        selected = load_takes(self.store.root, scene)
        self.assertEqual([item["takes"][0]["id"] for item in selected], [take["id"]])

    def test_a_retirement_still_reaches_takes_in_a_library_the_lab_has_not_migrated(self):
        take, scene = self.exportable_scene()
        self.store.retire("identity", self.identity["id"])
        self.downgrade_to_schema_eight()
        with self.assertRaisesRegex(ExportError, "retired"):
            load_takes(self.store.root, scene)
        db = sqlite3.connect(self.store.db_path)
        try:
            state = RetirementReader(db).state("take", take["id"])
        finally:
            db.close()
        self.assertEqual((state["source_kind"], state["inherited"]), ("identity", True))


class ExportUseTests(EditedTakeCase):
    def test_export_loads_an_edited_take_with_its_moved_word_times(self):
        original, scene = self.exportable_scene()
        edited = self.edit(original, {"start_seconds": 0.5, "end_seconds": 1.5})
        scene["lines"][0]["selected_takes"]["seg-1"] = edited["id"]
        loaded = load_takes(self.store.root, scene)[0]["takes"][0]
        self.assertEqual(loaded["id"], edited["id"])
        self.assertEqual([(item["word"], item["start"]) for item in loaded["word_timings"]["words"]],
                         [("Hold", 0.0), ("the", 0.2), ("line", 0.4), ("now", 0.8)])
        self.assertEqual(loaded["word_timings"]["audio_sha256"], edited["audio_sha256"])
        self.assertTrue(loaded["phoneme_timings"]["phonemes"])


class SceneUseTests(unittest.TestCase):
    """An edit of a scene's take is a take for the same line, so a scene can choose it and assemble it."""

    def setUp(self):
        self.store = temp_cast(self)
        self.character = self.store.create_character("Mira", "Warm and steady")
        self.identity = self.store.create_identity(self.character["id"], "qwen3-tts-1.7b-base", "Mira Base")
        self.reference = self.store.add_reference(
            self.identity["id"], "mira.wav", tone_wav(), transcript="A clear reference line.", origin="local fixture",
            allowed_use="tests only", edits={"target_sample_rate": RATE})
        self.store.update_identity(self.identity["id"], {"status": "approved", "reference_version_id": self.reference["id"]})
        self.scenes = SceneStore(self.store, {entry["id"]: entry for entry in read_catalog()["engines"]})

    def test_an_edited_take_can_be_chosen_for_a_scene_line_and_assembled(self):
        scene = self.scenes.create_scene({"schema_version": 1, "name": "Edited", "lines": [{
            "line_id": "line-001", "line_type": "dialogue", "character_id": self.character["id"],
            "identity_id": self.identity["id"], "reference_version_id": self.reference["id"], "text": "The road is clear.",
            "language": "English", "direction": "", "pause_before_ms": 0, "pause_after_ms": 0, "take_count": 1,
            "settings": {"seed": 42}, "use_pronunciations": False}]})
        generation = self.store.get_generation_identity(self.identity["id"], self.reference["id"])
        original = self.store.create_take(
            generation, line_id=f"{scene['scene_id'][:12]}:line-001", text="The road is clear.",
            spoken_text="The road is clear.", language="English", direction="", settings={"seed": 42},
            model={"backend": "fixture", "checkpoint_revision": "fixture"},
            wav=(0.1 * np.sin(2 * np.pi * 220 * np.arange(2 * RATE) / RATE)).astype(np.float32), sample_rate=RATE,
            generation_info={})
        self.scenes.select_take(scene["scene_id"], "line-001", original["id"])
        edited, _ = self.store.create_edited_take(original["id"], {"start_seconds": 0.5, "end_seconds": 1.5})
        self.scenes.select_take(scene["scene_id"], "line-001", edited["id"])
        chosen = self.scenes.get_scene(scene["scene_id"])
        self.assertEqual(chosen["lines"][0]["selected_takes"]["seg-001"], edited["id"])
        self.assertEqual(self.store.get_take(original["id"])["status"], "candidate")
        preview = self.scenes.assemble_preview(scene["scene_id"])
        self.assertAlmostEqual(preview["lines"][0]["duration_seconds"], 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
