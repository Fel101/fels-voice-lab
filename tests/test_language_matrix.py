import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import asr_worker
import language_matrix as lm
from evaluation_suites import get_suite

KOKORO_VOICES = ["af_heart", "af_bella", "bf_alice", "bf_emma", "ef_dora", "em_alex", "ff_siwis"]
QWEN_LANGUAGES = {"en", "zh", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"}
SCREENED_UPSTREAM = QWEN_LANGUAGES | {"hi", "ar"}


class ErrorRateTests(unittest.TestCase):
    def test_substitutions_deletions_and_insertions_each_count_once(self):
        reference = "the next train leaves"
        self.assertEqual(lm.error_rate(reference, reference, "wer"), 0)
        self.assertEqual(lm.error_rate(reference, "the next trains leaves", "wer"), 0.25)
        self.assertEqual(lm.error_rate(reference, "the next leaves", "wer"), 0.25)
        self.assertEqual(lm.error_rate(reference, "the next train leaves now", "wer"), 0.25)
        self.assertEqual(lm.error_rate(reference, "", "wer"), 1.0)

    def test_case_and_punctuation_do_not_count(self):
        self.assertEqual(lm.error_rate("Le prochain train part dans cinq minutes.",
                                       "le prochain train part dans cinq minutes", "wer"), 0)

    def test_hindi_marks_are_part_of_the_word_but_the_danda_is_not(self):
        reference = "अगली ट्रेन पाँच मिनट में छूटेगी।"
        self.assertEqual(lm.error_rate(reference, "अगली ट्रेन पाँच मिनट में छूटेगी", "wer"), 0)
        # The anusvara spelling is a different word, so one of six words is wrong.
        self.assertAlmostEqual(lm.error_rate(reference, "अगली ट्रेन पांच मिनट में छूटेगी", "wer"), 1 / 6)

    def test_chinese_is_scored_by_character(self):
        reference = "下一班火车五分钟后出发。"
        self.assertEqual(lm.error_rate(reference, "下一班火车五分钟后出发", "cer"), 0)
        self.assertAlmostEqual(lm.error_rate(reference, "下一班火车五分钟后出法", "cer"), 1 / 11)
        self.assertEqual(lm.metric_for("zh"), "cer")
        self.assertEqual(lm.metric_for("fr"), "wer")

    def test_a_reference_without_text_is_rejected(self):
        with self.assertRaises(ValueError):
            lm.error_rate("...", "anything", "wer")


class ThresholdTests(unittest.TestCase):
    def test_classification_uses_the_fixed_thresholds(self):
        self.assertEqual(lm.classify(0.2), "understood")
        self.assertEqual(lm.classify(0.21), "partial")
        self.assertEqual(lm.classify(0.4), "partial")
        self.assertEqual(lm.classify(0.41), "not_understood")
        self.assertEqual((lm.UNDERSTOOD_MAX, lm.PARTIAL_MAX), (0.20, 0.40))


class ClaimTests(unittest.TestCase):
    def test_each_engine_claims_the_columns_its_allowlist_and_documents_name(self):
        self.assertEqual(lm.claimed_columns(lm.claims("kokoro")), {"en", "en-GB", "es", "fr", "hi", "it", "pt"})
        self.assertEqual(lm.claimed_columns(lm.claims("turbo")), {"en"})
        self.assertEqual(lm.claimed_columns(lm.claims("orig")), {"en"})
        self.assertEqual(lm.claimed_columns(lm.claims("breeze-tts-2")), {"en", "zh"})
        for engine in ("qwen3-tts-1.7b-voice-design", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base",
                       "qwen3-tts-1.7b-custom-voice"):
            self.assertEqual(lm.claimed_columns(lm.claims(engine)), QWEN_LANGUAGES, engine)
        self.assertEqual(lm.claimed_columns(lm.claims("voxcpm2")), SCREENED_UPSTREAM)
        self.assertEqual(lm.claimed_columns(lm.claims("drama-3")), SCREENED_UPSTREAM)

    def test_claims_beyond_the_screen_are_listed_not_hidden(self):
        drama_unscreened = [label for label in lm.claimed_labels(lm.claims("drama-3"))
                            if label not in lm.COLUMN_OF_LABEL]
        self.assertEqual(len(drama_unscreened), 71)  # 83 Drama labels, of which 12 are screened here
        voxcpm = lm.claims("voxcpm2")
        self.assertEqual(voxcpm["scene_allowlist"], ["English"])
        self.assertEqual(len(voxcpm["upstream"]), 30)

    def test_kokoro_voice_follows_the_language_letter_and_prefers_the_app_default(self):
        self.assertEqual(lm.kokoro_voice("en", KOKORO_VOICES), "af_heart")
        self.assertEqual(lm.kokoro_voice("en-GB", KOKORO_VOICES), "bf_alice")
        self.assertEqual(lm.kokoro_voice("es", KOKORO_VOICES), "ef_dora")
        self.assertEqual(lm.kokoro_voice("fr", KOKORO_VOICES), "ff_siwis")
        for column in ("hi", "it", "pt"):
            self.assertIsNone(lm.kokoro_voice(column, KOKORO_VOICES), column)

    def test_suite_has_one_case_per_column_with_three_repeats(self):
        suite = get_suite(lm.SUITE_ID)
        self.assertEqual(suite["repeat_count"], 3)
        labels = [case["language"] for case in suite["cases"]]
        self.assertEqual(sorted(labels), sorted(label for _, label, _ in lm.COLUMNS))
        self.assertTrue(all(case["text"].strip() for case in suite["cases"]))


class PlanTests(unittest.TestCase):
    def test_plan_repeats_each_claimed_cell_and_marks_kokoro_cells_without_voices(self):
        plan = lm.build_plan(suite=get_suite(lm.SUITE_ID), engines=["kokoro"], voices=KOKORO_VOICES)
        self.assertEqual(len(plan), 7 * 3)
        skipped = {clip["column"] for clip in plan if clip["skip"]}
        self.assertEqual(skipped, {"hi", "it", "pt"})
        self.assertEqual(len({clip["id"] for clip in plan}), len(plan))

    def test_requests_carry_only_the_fields_each_engine_accepts(self):
        suite = get_suite(lm.SUITE_ID)
        plan = lm.build_plan(suite=suite, engines=list(lm.ENGINE_ORDER), columns=["en", "zh"], repeats=1,
                             voices=KOKORO_VOICES)
        clips = {(clip["engine"], clip["column"]): clip for clip in plan}
        loaded = {"supported_speakers": ["Vivian", "Ryan"]}
        design = lm.request_for(clips[("qwen3-tts-1.7b-voice-design", "zh")], loaded)
        self.assertEqual(design["language"], "Chinese")
        self.assertIn("voice_description", design)
        self.assertNotIn("reference_path", design)
        breeze = lm.request_for(clips[("breeze-tts-2", "zh")], loaded)
        self.assertEqual(breeze["mode"], "design")
        self.assertNotIn("language", breeze)
        clone = lm.request_for(clips[("qwen3-tts-1.7b-base", "zh")], loaded)
        self.assertTrue(clone["xvec_only"])
        self.assertTrue(clone["reference_path"].endswith("af_heart.wav"))
        self.assertEqual(clone["language"], "Chinese")
        custom = lm.request_for(clips[("qwen3-tts-1.7b-custom-voice", "zh")], loaded)
        self.assertEqual((custom["speaker"], custom["language"]), ("Vivian", "Chinese"))
        turbo = lm.request_for(clips[("turbo", "en")], loaded)
        self.assertEqual(set(turbo), {"engine", "text"})
        drama = lm.request_for(clips[("drama-3", "zh")], loaded)
        self.assertEqual(drama["language"], "Chinese")
        kokoro = lm.request_for(clips[("kokoro", "en")], loaded)
        self.assertEqual((kokoro["voice"], kokoro["language"]), ("af_heart", "American English"))


class FakePool:
    def __init__(self, entries, *, fail_load=(), supported=None):
        self.entries = entries
        self.device = "cuda"
        self.fail_load = set(fail_load)
        self.supported = supported or {}
        self.unloads = 0
        self.spoken = []

    def load(self, key):
        if key in self.fail_load:
            raise RuntimeError("worker exited early\nlog: out/worker.log")
        return {"load_seconds": 0.5, "supported_languages": self.supported.get(key, []),
                "supported_speakers": ["Vivian", "Ryan"]}

    def speak(self, request):
        self.spoken.append(request)
        samples = np.full(24000, 0.25, dtype=np.float32)
        return samples, 24000, {"total_s": 0.8, "device": "Vulkan1" if request["engine"] == "drama-3" else None}

    def unload(self):
        self.unloads += 1


def _installed(entry):
    return {"installed": True, "missing": [], "environment": None, "checkpoint": None}


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.suite = get_suite(lm.SUITE_ID)

    def test_generate_writes_clips_and_records_each_outcome(self):
        plan = lm.build_plan(suite=self.suite, engines=["kokoro", "qwen3-tts-1.7b-custom-voice"],
                             columns=["en", "zh", "hi"], repeats=1, voices=KOKORO_VOICES)
        pool = FakePool({"kokoro": {}, "qwen3-tts-1.7b-custom-voice": {}},
                        supported={"qwen3-tts-1.7b-custom-voice": ["English"]})
        records = lm.generate(pool, plan, self.out, installed=_installed)
        by_id = {record["id"]: record for record in records}
        self.assertEqual(by_id["kokoro:en:1"]["status"], "ok")
        self.assertTrue((self.out / by_id["kokoro:en:1"]["wav"]).is_file())
        self.assertEqual(by_id["kokoro:hi:1"]["status"], "unavailable")
        self.assertIn("voice pack", by_id["kokoro:hi:1"]["reason"])
        self.assertEqual(by_id["qwen3-tts-1.7b-custom-voice:en:1"]["status"], "ok")
        self.assertEqual(by_id["qwen3-tts-1.7b-custom-voice:zh:1"]["status"], "unavailable")
        self.assertIn("does not report Chinese", by_id["qwen3-tts-1.7b-custom-voice:zh:1"]["reason"])
        self.assertEqual(pool.unloads, 2)  # one unload per engine that was loaded

    def test_a_failed_load_fails_that_engines_clips_and_the_run_continues(self):
        plan = lm.build_plan(suite=self.suite, engines=["kokoro", "turbo"], columns=["en"], repeats=2,
                             voices=KOKORO_VOICES)
        pool = FakePool({"kokoro": {}, "turbo": {}}, fail_load={"kokoro"})
        records = lm.generate(pool, plan, self.out, installed=_installed)
        statuses = {(record["engine"], record["status"]) for record in records}
        self.assertEqual(statuses, {("kokoro", "failed"), ("turbo", "ok")})
        failed = next(record for record in records if record["status"] == "failed")
        self.assertEqual(failed["reason"], "RuntimeError: worker exited early")

    def test_an_engine_that_is_not_installed_is_never_loaded(self):
        plan = lm.build_plan(suite=self.suite, engines=["turbo"], columns=["en"], repeats=1)
        pool = FakePool({"turbo": {}})
        records = lm.generate(pool, plan, self.out,
                              installed=lambda entry: {"installed": False, "missing": ["hf-cache/turbo"]})
        self.assertEqual(records[0]["status"], "unavailable")
        self.assertIn("not installed: hf-cache/turbo", records[0]["reason"])
        self.assertEqual(pool.spoken, [])
        self.assertEqual(pool.unloads, 0)


class ScoreTests(unittest.TestCase):
    def test_transcribe_sends_forced_languages_and_score_uses_the_reference(self):
        out = Path(tempfile.mkdtemp())
        records = [
            {"id": "a", "engine": "kokoro", "column": "fr", "status": "ok", "text": "Le prochain train part dans cinq minutes.", "wav": "kokoro/fr-1.wav"},
            {"id": "b", "engine": "breeze-tts-2", "column": "zh", "status": "ok", "text": "下一班火车五分钟后出发。", "wav": "breeze/zh-1.wav"},
            {"id": "c", "engine": "turbo", "column": "en", "status": "failed", "text": "The next train leaves in five minutes.", "wav": None},
        ]
        seen = {}

        def fake_run(manifest):
            seen["items"] = json.loads(manifest.read_text(encoding="utf-8"))
            return {"a": "le prochain train part dans cinq minute", "b": "下一班火车五分钟后出发"}

        lm.transcribe(records, out, run_asr=fake_run)
        lm.score(records)
        self.assertEqual([item["language"] for item in seen["items"]], ["fr", "zh"])
        self.assertEqual(records[0]["error_rate"], round(1 / 7, 4))  # seven words; "minutes" became "minute"
        self.assertEqual(records[0]["metric"], "wer")
        self.assertEqual(records[1]["error_rate"], 0)
        self.assertEqual(records[1]["metric"], "cer")
        self.assertNotIn("error_rate", records[2])

    def test_asr_manifest_runs_each_clip_with_its_language_forced(self):
        calls = []

        class FakeModel:
            def transcribe(self, audio, **options):
                calls.append((audio, options["language"], options["vad_filter"]))
                return ([type("Segment", (), {"text": " hola "})()], None)

        items = asr_worker.transcribe_manifest(FakeModel(), [{"id": "x", "audio": "clip.wav", "language": "es"}])
        self.assertEqual(items, [{"id": "x", "text": "hola"}])
        self.assertEqual(calls, [("clip.wav", "es", False)])


class CellTests(unittest.TestCase):
    def clip(self, error, status="ok", device="cuda", reason=None):
        return {"status": status, "error_rate": error, "metric": "wer", "device": device, "reason": reason}

    def test_the_median_decides_the_status_and_missing_repeats_are_visible(self):
        summary = lm.cell([self.clip(0.1), self.clip(0.5), self.clip(0.05)], 3)
        self.assertEqual((summary["status"], summary["median"], summary["n"]), ("understood", 0.1, 3))
        partial = lm.cell([self.clip(0.3), self.clip(0.35), self.clip(None, status="failed", reason="x")], 3)
        self.assertEqual((partial["status"], partial["n"], partial["of"]), ("partial", 2, 3))

    def test_no_spoken_clip_is_failed_or_unavailable_by_cause(self):
        self.assertEqual(lm.cell([self.clip(None, "failed", reason="boom")], 3)["status"], "failed")
        unavailable = lm.cell([self.clip(None, "unavailable", reason="no voice")], 3)
        self.assertEqual((unavailable["status"], unavailable["reason"]), ("unavailable", "no voice"))
        self.assertEqual(lm.cell([], 3)["status"], "not_run")


def _evidence_fixture():
    suite = get_suite(lm.SUITE_ID)
    records = []
    for repeat, error in ((1, 0.05), (2, 0.08), (3, 0.04)):
        records.append({"id": f"kokoro:en:{repeat}", "engine": "kokoro", "column": "en", "case_id": "multilingual-english",
                        "text": "The next train leaves in five minutes.", "repeat": repeat, "seed": 42 + repeat,
                        "status": "ok", "reason": None, "device": "cuda", "total_s": 1.0, "audio_seconds": 2.0,
                        "load_seconds": 1.0, "wav": None, "heard": "x", "metric": "wer", "error_rate": error})
    records.append({"id": "kokoro:hi:1", "engine": "kokoro", "column": "hi", "case_id": "multilingual-hindi",
                    "text": "अगली ट्रेन पाँच मिनट में छूटेगी।", "repeat": 1, "seed": 43, "status": "unavailable",
                    "reason": "no Kokoro voice pack for this language in the installed snapshot", "device": None,
                    "total_s": None, "audio_seconds": None, "load_seconds": None, "wav": None})
    return lm.evidence(records, engines=["kokoro"], repeats=3, suite=suite, started="2026-10-22T10:00:00+00:00",
                       finished="2026-10-22T10:30:00+00:00", device="cuda", names={"kokoro": "Kokoro 82M"},
                       gpu="Test GPU")


class RenderTests(unittest.TestCase):
    def test_render_shows_each_cell_its_status_and_the_claims_behind_it(self):
        text = lm.render(_evidence_fixture())
        self.assertIn("# Language screen", text)
        self.assertIn("| Kokoro 82M | cuda | 0.05 understood |", text)  # median of 0.05, 0.08, 0.04 is 0.05
        self.assertIn("unavailable", text)
        self.assertIn("Scene picker: American English", text)
        self.assertIn("Not run", text)
        self.assertIn("## Method", text)
        self.assertIn("Test GPU", text)

    def test_render_is_deterministic(self):
        self.assertEqual(lm.render(_evidence_fixture()), lm.render(_evidence_fixture()))


class ScriptTests(unittest.TestCase):
    def test_a_transcript_in_other_letters_is_not_a_word_error(self):
        hindi = "अगली ट्रेन पाँच मिनट में छूटेगी।"
        self.assertTrue(lm.script_mismatch(hindi, "Agali train 5 minutes."))
        self.assertFalse(lm.script_mismatch(hindi, "अगली ट्रेन पाँच मिनट में छूटेगी"))
        self.assertFalse(lm.script_mismatch("下一班火车马上就要离开车站了。", "下一班火车马上就要离开车站了"))
        self.assertFalse(lm.script_mismatch("The next train", "The next train 5"))  # digits are not letters
        self.assertFalse(lm.script_mismatch("...", "anything at all"))  # nothing to compare

    def test_scoring_marks_the_clip_and_its_cell_inconclusive(self):
        records = [{"id": "h", "engine": "drama-3", "column": "hi", "status": "ok",
                    "text": "अगली ट्रेन पाँच मिनट में छूटेगी।", "heard": "Agali train 5 minutes."}]
        lm.score(records)
        self.assertIsNone(records[0]["error_rate"])
        self.assertIn("latin letters for devanagari speech", records[0]["reason"])
        summary = lm.cell(records, 1)
        self.assertEqual(summary["status"], "inconclusive")
        self.assertEqual(lm._cell_text(summary), "inconclusive")


class ResumeTests(unittest.TestCase):
    def test_saved_records_are_scored_on_resume_without_speaking_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = {"started": "2026-10-22T10:00:00+00:00", "device": "cuda", "gpu": "Test GPU", "engines": ["kokoro"],
                    "repeats": 1, "names": {"kokoro": "Kokoro 82M"}}
            records = [{"id": "kokoro:en:1", "engine": "kokoro", "column": "en", "label": "American English",
                        "suite_label": "English", "case_id": "multilingual-english",
                        "text": "The next train is about to leave the station.", "repeat": 1, "seed": 43,
                        "voice": "af_heart", "status": "ok", "reason": None, "device": "cuda", "total_s": 0.8,
                        "audio_seconds": 2.6, "load_seconds": 26.0, "wav": "kokoro/en-1.wav"}]
            lm.save_records(out, meta, records)
            self.assertEqual(lm.load_records(out), (meta, records))
            heard = {"kokoro:en:1": "The next train is about to leave the station."}
            evidence_file = lm.finish(records, meta=meta, out_dir=out, suite=get_suite(lm.SUITE_ID),
                                      run_asr=lambda manifest: heard)
            report = json.loads(evidence_file.read_text(encoding="utf-8"))
            self.assertEqual(report["engines"][0]["cells"]["en"]["median"], 0.0)
            self.assertEqual(report["clips"][0]["error_rate"], 0.0)


class PublishTests(unittest.TestCase):
    def test_sanitize_replaces_this_machines_folders(self):
        home = str(Path.home())
        text = f"worker log: {lm.LAB / 'out' / 'worker.log'} under {home}\\AppData"
        self.assertEqual(lm.sanitize({"reason": text, "nested": [text]}),
                         {"reason": "worker log: voice-lab\\out\\worker.log under ~\\AppData",
                          "nested": ["worker log: voice-lab\\out\\worker.log under ~\\AppData"]})

    def test_a_partial_run_is_refused_before_anything_is_written(self):
        report = _evidence_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "evidence.json"
            source.write_text(json.dumps(report), encoding="utf-8")
            target = Path(tmp) / "published.json"
            doc = Path(tmp) / "LANGUAGE_MATRIX.md"
            with self.assertRaises(SystemExit) as raised:
                lm.publish(source, evidence_path=target, doc_path=doc)
            self.assertIn("Not a full run", str(raised.exception))
            self.assertFalse(target.exists())
            self.assertFalse(doc.exists())

    def test_coverage_counts_every_claimed_clip(self):
        problems = lm.coverage_problems(_evidence_fixture())
        self.assertTrue(any("missing" in problem for problem in problems))
        self.assertTrue(any("clips recorded" in problem for problem in problems))


# Scene-picker entries the screen did not clear. The app keeps them and marks each one unscreened (docs/LANGUAGE_MATRIX.md).
UNSCREENED_PICKER_ENTRIES = {
    ("qwen3-tts-1.7b-voice-design", "Italian"), ("qwen3-tts-1.7b-voice-design", "Portuguese"),
    ("qwen3-tts-1.7b-base", "Japanese"), ("qwen3-tts-1.7b-base", "Portuguese"),
    ("qwen3-tts-0.6b-base", "Portuguese"),
    ("qwen3-tts-1.7b-custom-voice", "French"), ("qwen3-tts-1.7b-custom-voice", "Italian"),
    ("qwen3-tts-1.7b-custom-voice", "Japanese"), ("qwen3-tts-1.7b-custom-voice", "Portuguese"),
}


class UnscreenedMarkTests(unittest.TestCase):
    @unittest.skipUnless(lm.EVIDENCE.is_file(), "no published language screen yet")
    def test_the_app_marks_exactly_the_uncleared_picker_entries(self):
        marked = {(engine, label) for engine in lm.ENGINE_ORDER for label in lm.unscreened_languages(engine)}
        self.assertEqual(marked, UNSCREENED_PICKER_ENTRIES)

    def test_a_missing_screen_marks_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lm.unscreened_languages("qwen3-tts-1.7b-base", path=Path(tmp) / "missing.json"), [])

    @unittest.skipUnless(lm.EVIDENCE.is_file(), "no published language screen yet")
    def test_the_picker_payload_carries_the_marks_for_every_engine(self):
        # VoiceDesign has no performance controls, so its marks must not depend on the per-engine schema.
        import serve
        self.assertEqual(serve.unscreened_by_engine(lm.ENGINE_ORDER), {
            "qwen3-tts-1.7b-voice-design": ["Italian", "Portuguese"],
            "qwen3-tts-1.7b-base": ["Japanese", "Portuguese"],
            "qwen3-tts-0.6b-base": ["Portuguese"],
            "qwen3-tts-1.7b-custom-voice": ["French", "Italian", "Japanese", "Portuguese"],
        })
        self.assertIn("Portuguese", serve.performance_schema("qwen3-tts-1.7b-base", {"capabilities": {}})["languages"])


class PublishedEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(lm.EVIDENCE.is_file() and lm.PUBLIC_DOC.is_file(), "no published language screen yet")
    def test_the_public_matrix_is_rendered_from_the_recorded_evidence(self):
        report = json.loads(lm.EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(lm.PUBLIC_DOC.read_text(encoding="utf-8"), lm.render(report))

    @unittest.skipUnless(lm.EVIDENCE.is_file(), "no published language screen yet")
    def test_unscreened_entries_are_exactly_the_uncleared_picker_entries(self):
        # A new gap, or a closed one, must change this register together with the marks and docs/LANGUAGE_MATRIX.md.
        report = json.loads(lm.EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual({(gap["engine"], gap["label"]) for gap in lm.picker_gaps(report)}, UNSCREENED_PICKER_ENTRIES)

    @unittest.skipUnless(lm.EVIDENCE.is_file(), "no published language screen yet")
    def test_the_published_picker_is_the_live_picker(self):
        # Changing an allowlist without publishing a new screen would leave the public page describing the old app.
        report = json.loads(lm.EVIDENCE.read_text(encoding="utf-8"))
        for row in report["engines"]:
            self.assertEqual(row["claims"]["scene_allowlist"], lm.claims(row["engine"])["scene_allowlist"], row["engine"])


if __name__ == "__main__":
    unittest.main()
