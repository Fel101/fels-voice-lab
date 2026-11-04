import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import kokoro_support
from kokoro_support import (KOKORO_LANGUAGES, discover_kokoro_voices,
                            normalize_kokoro_voices, resolve_kokoro_language)
from scene_store import SUPPORTED_LANGUAGES


class KokoroM7ContractTests(unittest.TestCase):
    def test_cached_voice_discovery_uses_local_model_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "voices"
            folder.mkdir()
            for name in ("af_heart", "am_fenrir", "ef_dora", "ff_siwis"):
                (folder / (name + ".pt")).write_bytes(b"discovery fixture; not a checkpoint")
            with patch("kokoro_support.kokoro_snapshot_dir", return_value=Path(temp)):
                voices = discover_kokoro_voices()
        self.assertIn("af_heart", voices)
        self.assertIn("am_fenrir", voices)
        self.assertIn("ef_dora", voices)
        self.assertIn("ff_siwis", voices)

    def test_equal_blend_is_canonical_and_rejects_missing_or_duplicate_voices(self):
        self.assertEqual(normalize_kokoro_voices("am_fenrir,af_heart", ["af_heart", "am_fenrir"]),
                         ["af_heart", "am_fenrir"])
        with self.assertRaisesRegex(ValueError, "more than once"):
            normalize_kokoro_voices("af_heart,af_heart", ["af_heart"])
        with self.assertRaisesRegex(ValueError, "not cached locally"):
            normalize_kokoro_voices("af_missing", ["af_heart"])

    def test_language_routing_supports_aliases_and_infers_british_pipeline(self):
        self.assertEqual(resolve_kokoro_language("English", ["bf_emma"]), "b")
        self.assertEqual(resolve_kokoro_language("English", ["af_heart", "am_fenrir"]), "a")
        self.assertEqual(resolve_kokoro_language("es", ["ef_dora"]), "e")
        self.assertEqual(resolve_kokoro_language("French", ["ff_siwis"]), "f")

    def test_scene_schema_lists_the_kokoro_language_labels(self):
        # Presence in the allowlist only; whether a label has a voice pack or a screen is in docs/LANGUAGE_MATRIX.md.
        for language in ("American English", "British English", "Spanish", "French", "Hindi",
                         "Italian", "Brazilian Portuguese"):
            self.assertIn(language, SUPPORTED_LANGUAGES["kokoro"])
        self.assertIn("Japanese", KOKORO_LANGUAGES["j"]["name"])

    def test_a_language_package_counts_when_kokoros_own_environment_holds_it(self):
        # The service's .venv has no Kokoro packages; it looks in .venv-kokoro without importing anything.
        with tempfile.TemporaryDirectory() as temp:
            site = Path(temp) / "Lib" / "site-packages"
            site.mkdir(parents=True)
            with patch.object(kokoro_support, "KOKORO_SITE_PACKAGES", site):
                rows = {row["code"]: row for row in kokoro_support.kokoro_languages()}
                self.assertEqual((rows["j"]["available"], rows["j"]["missing_module"]), (False, "pyopenjtalk"))
                with self.assertRaisesRegex(ValueError, "pyopenjtalk"):
                    resolve_kokoro_language("Japanese", [])
                (site / "pyopenjtalk").mkdir()
                (site / "pyopenjtalk" / "__init__.py").write_text("raise ImportError('never imported')\n", encoding="utf-8")
                rows = {row["code"]: row for row in kokoro_support.kokoro_languages()}
                self.assertEqual((rows["j"]["available"], rows["j"]["missing_module"]), (True, None))
                self.assertEqual(resolve_kokoro_language("Japanese", []), "j")
        self.assertTrue(rows["a"]["available"])

    def test_the_kokoro_environment_folder_is_the_one_the_catalog_runs_kokoro_from(self):
        catalog = json.loads((Path(kokoro_support.LAB) / "catalog" / "engines.json").read_text(encoding="utf-8"))
        python = next(entry for entry in catalog["engines"] if entry["id"] == "kokoro")["runtime"]["python"]
        self.assertEqual(kokoro_support.KOKORO_SITE_PACKAGES, Path(kokoro_support.LAB) / Path(python).parent.parent / "Lib" / "site-packages")

    def test_a_missing_voice_pack_points_at_the_installer(self):
        with self.assertRaisesRegex(ValueError, r"setup-kokoro\.bat --voices af_missing bm_gone"):
            normalize_kokoro_voices("af_missing,bm_gone", ["af_heart"])


class KokoroWorkerPhonemeTests(unittest.TestCase):
    """The phonemes come from Kokoro's worker, which holds the language rules; nothing needs the model loaded."""

    def test_the_kokoro_worker_answers_without_a_loaded_model(self):
        from worker_runtime import EngineWorkerRuntime
        runtime = EngineWorkerRuntime("kokoro", "cpu")
        with patch("kokoro_support.phonemize_kokoro", return_value={"language": "a", "segments": []}) as phonemize:
            result, stop = runtime.dispatch("phonemize", {"text": "Hello", "language": "English"})
        self.assertEqual((result, stop), ({"language": "a", "segments": []}, False))
        phonemize.assert_called_once_with("Hello", "English")
        self.assertIsNone(runtime.engine)

    def test_another_engines_worker_refuses(self):
        from worker_runtime import EngineWorkerRuntime
        with self.assertRaisesRegex(ValueError, "Only Kokoro"):
            EngineWorkerRuntime("phonon-31m-en", "cpu").phonemize({"text": "Hello"})

    def test_the_protocol_carries_the_method(self):
        import worker_protocol
        self.assertIn("phonemize", worker_protocol.METHODS)
        self.assertEqual(worker_protocol.request(1, "phonemize", text="Hi")["method"], "phonemize")


class PoolPhonemeTests(unittest.TestCase):
    class Worker:
        def __init__(self):
            self.calls = []
            self.alive = True
            self.pid = 1

        def start(self):
            pass

        def call(self, method, **params):
            self.calls.append((method, params))
            if method == "load":
                return {"pid": 1}
            return {"language": "b", "segments": [{"graphemes": params["text"], "phonemes": "x"}]}

        def close(self, force=False):
            self.alive = False

    def pool(self, worker):
        from worker_pool import EnginePool
        pool = EnginePool("cpu", 0, worker_factory=lambda entry: worker, idle_poll_seconds=0.1)
        self.addCleanup(pool.close)
        return pool

    def test_the_text_goes_to_the_kokoro_worker_which_is_loaded_first(self):
        worker = self.Worker()
        result = self.pool(worker).phonemize("Hello there", "British English")
        self.assertEqual(result["segments"][0]["graphemes"], "Hello there")
        self.assertEqual([method for method, _params in worker.calls], ["load", "phonemize"])
        self.assertEqual(worker.calls[1][1], {"text": "Hello there", "language": "British English"})

    def test_a_text_or_language_it_cannot_take_is_refused_before_any_worker_starts(self):
        worker = self.Worker()
        pool = self.pool(worker)
        for text, language, message in (("   ", "English", "non-whitespace"), ("x" * 1201, "English", "under 1200"),
                                        ("Hi", "Klingon", "Unsupported Kokoro language")):
            with self.subTest(language=language, length=len(text)):
                with self.assertRaisesRegex(ValueError, message):
                    pool.phonemize(text, language)
        self.assertEqual(worker.calls, [])


if __name__ == "__main__":
    unittest.main()
