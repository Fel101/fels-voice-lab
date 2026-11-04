"""phonon_support.py: the voice list read from disk and the checks a Phonon request goes through. No ptts and no model."""
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import performance_settings  # noqa: E402
import phonon_support as ps  # noqa: E402
from phonon_fixtures import Lab, VOICES, record  # noqa: E402,F401

CATALOG = json.loads((ROOT / "catalog" / "engines.json").read_text("utf-8"))
ENTRY = next(entry for entry in CATALOG["engines"] if entry["id"] == ps.PHONON_ENGINE_ID)


class CatalogPinTests(unittest.TestCase):
    def test_the_pins_are_the_catalogs_and_the_snapshots_sit_where_the_hub_cache_puts_them(self):
        found = ps.pins()
        self.assertEqual(found["model"], (ENTRY["checkpoint"], ENTRY["local_revision"]))
        self.assertEqual(found["voices"], (ENTRY["backend"]["voices_checkpoint"], ENTRY["backend"]["voices_revision"]))
        model, voices = ps.snapshot_dirs(ROOT)
        self.assertEqual(model.relative_to(ROOT).as_posix(),
                         f"hf-cache/hub/models--gradium--phonon-31m-en/snapshots/{ENTRY['local_revision']}")
        self.assertEqual(voices.relative_to(ROOT).as_posix(),
                         f"hf-cache/hub/models--gradium--phonon-voices/snapshots/{ENTRY['backend']['voices_revision']}")

    def test_the_defaults_and_ranges_agree_with_the_catalog_and_the_settings_table(self):
        caps = ENTRY["capabilities"]
        self.assertEqual(caps["sampling_controls"]["defaults"], {"seed": ps.DEFAULTS["seed"], "temperature": ps.DEFAULTS["temperature"]})
        self.assertEqual(sorted(caps["sampling_controls"]["controls"]), ["seed", "temperature"])
        pace = next(item for item in caps["acting_controls"] if item["id"] == "duration_delta")
        self.assertEqual((pace["minimum"], pace["maximum"]), ps.DURATION_DELTA_RANGE)
        self.assertEqual(performance_settings.DEFAULTS[ps.PHONON_ENGINE_ID], ps.DEFAULTS)
        table = performance_settings.CONTROL_UI[ps.PHONON_ENGINE_ID]
        self.assertEqual(set(table), set(ps.DEFAULTS))
        self.assertEqual(table["seed"][1:3], ps.SEED_RANGE)
        self.assertEqual(table["temperature"][1:3], ps.TEMPERATURE_RANGE)
        self.assertEqual(table["duration_delta"][1:3], ps.DURATION_DELTA_RANGE)

    def test_the_catalog_says_what_the_engine_cannot_do(self):
        caps = ENTRY["capabilities"]
        self.assertEqual(caps["voice_cloning"]["status"], "unsupported", "the pool refuses reference audio only for this exact status")
        self.assertEqual(caps["voice_direction"]["status"], "unsupported")
        self.assertEqual(caps["voice_design"]["status"], "unsupported_by_checkpoint")
        self.assertEqual(caps["voice_selection"]["status"], "worker_verified")
        self.assertEqual(ENTRY["integration"], {"status": "integrated", "service_api": True})
        self.assertEqual(ENTRY["runtime"]["environment"], ".venv-phonon", "Phonon has an environment of its own")
        self.assertIsNone(ENTRY["backend"].get("device"), "the device is the runtime's (cpu), not a backend option")
        self.assertEqual(ENTRY["runtime"]["device"], "cpu")


class VoiceListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_the_voices_are_the_english_entries_in_american_british_character_order_and_name_order(self):
        Lab(self.folder)
        voices = ps.discover_phonon_voices(self.folder)
        self.assertEqual([item["name"] for item in voices],
                         ["easton", "Harper", "Zoey", "Alfie", "Maeve", "Azgorath", "Old Mortlake"])
        self.assertEqual([item["group"] for item in voices], ["American English"] * 3 + ["British English"] * 2 + ["Characters"] * 2)
        self.assertEqual([item["character"] for item in voices], [False] * 5 + [True] * 2)
        self.assertEqual(ps.voice_names(self.folder), [item["name"] for item in voices])

    def test_each_voice_carries_what_the_pickers_show(self):
        Lab(self.folder)
        zoey = next(item for item in ps.discover_phonon_voices(self.folder) if item["name"] == "Zoey")
        self.assertEqual(zoey, {"name": "Zoey", "group": "American English", "character": False, "gender": "female",
                                "age": "Adult", "locale": "en-us", "description": "Zoey speaks."})
        mortlake = next(item for item in ps.discover_phonon_voices(self.folder) if item["name"] == "Old Mortlake")
        self.assertEqual((mortlake["gender"], mortlake["age"], mortlake["locale"]), (None, None, "characters"))

    def test_a_voice_whose_file_is_not_installed_is_left_out(self):
        lab = Lab(self.folder)
        (lab.voices / "voices" / "zoey.safetensors").unlink()
        self.assertNotIn("Zoey", ps.voice_names(self.folder))
        self.assertEqual(len(ps.voice_names(self.folder)), 6)

    def test_lines_that_are_not_usable_records_are_skipped_and_the_rest_still_load(self):
        Lab(self.folder, records=["this is not json", "[1, 2]", "", json.dumps(record("Nameless", filename=None) | {"name": " "}),
                                  json.dumps({"name": "NoFile", "language": "en", "locale": "en-us"}),
                                  json.dumps(record("NumberFile", filename=7)), record("Zoey")])
        self.assertEqual(ps.voice_names(self.folder), ["Zoey"])

    def test_a_filename_that_leaves_the_voice_folder_is_not_followed(self):
        lab = Lab(self.folder, records=[record("Zoey"), record("Sneaky", filename="../../outside.safetensors"),
                                        record("Absolute", filename=(self.folder / "elsewhere.safetensors").as_posix())])
        (lab.voices / ".." / ".." / "outside.safetensors").resolve().write_bytes(b"x")
        (self.folder / "elsewhere.safetensors").write_bytes(b"x")
        self.assertEqual(ps.voice_names(self.folder), ["Zoey"])

    def test_an_unknown_locale_counts_as_american_rather_than_vanishing(self):
        Lab(self.folder, records=[record("Odd", locale="en-au")])
        self.assertEqual([item["group"] for item in ps.discover_phonon_voices(self.folder)], ["American English"])

    def test_no_catalog_means_no_voices_not_an_error(self):
        Lab(self.folder, records=None)
        self.assertEqual(ps.discover_phonon_voices(self.folder), [])
        self.assertEqual(ps.voice_names(self.folder), [])

    def test_missing_files_names_what_is_absent_below_the_hub_cache(self):
        lab = Lab(self.folder)
        missing = ps.missing_files(self.folder)
        self.assertEqual(len(missing), len(ps.MODEL_FILES))
        for name in ps.MODEL_FILES:
            self.assertIn(f"models--gradium--phonon-31m-en/snapshots/{ENTRY['local_revision']}/{name}", missing)
        for name in ps.MODEL_FILES:
            (lab.model / name).parent.mkdir(parents=True, exist_ok=True)
            (lab.model / name).write_bytes(b"x")
        self.assertEqual(ps.missing_files(self.folder), [])
        (lab.voices / ps.VOICE_CATALOG).unlink()
        self.assertEqual(ps.missing_files(self.folder),
                         [f"models--gradium--phonon-voices/snapshots/{ENTRY['backend']['voices_revision']}/{ps.VOICE_CATALOG}"])


class VoiceChoiceTests(unittest.TestCase):
    NAMES = ["Zoey", "Harper", "Old Mortlake"]

    def test_the_default_is_harper_when_installed_and_the_first_voice_otherwise(self):
        self.assertEqual(ps.default_voice(self.NAMES), "Harper")
        self.assertEqual(ps.default_voice(["Zoey", "Alfie"]), "Zoey")

    def test_with_no_voices_the_answer_is_the_install_instruction(self):
        for call in (lambda: ps.default_voice([]), lambda: ps.resolve_voice(None, []), lambda: ps.resolve_voice("Harper", [])):
            with self.assertRaisesRegex(ValueError, r"Phonon 31M is not installed.*Install on its card.*setup-phonon\.bat"):
                call()

    def test_a_voice_is_matched_by_name_without_regard_to_case_or_outer_spaces(self):
        self.assertEqual(ps.resolve_voice("  old MORTLAKE ", self.NAMES), "Old Mortlake")
        self.assertEqual(ps.resolve_voice("zoey", self.NAMES), "Zoey")

    def test_no_voice_named_means_the_default(self):
        for value in (None, "", "   "):
            self.assertEqual(ps.resolve_voice(value, self.NAMES), "Harper")

    def test_an_unknown_voice_is_refused_with_the_count_and_a_few_names(self):
        with self.assertRaisesRegex(ValueError, r"no installed voice named 'Nobody'. 3 are installed, for example Zoey, Harper, Old Mortlake;"
                                                r" GET /api/info lists them all"):
            ps.resolve_voice("Nobody", self.NAMES)

    def test_a_voice_must_be_a_name(self):
        for value in (7, ["Harper"], {"name": "Harper"}, True):
            with self.assertRaisesRegex(ValueError, "chosen by its name"):
                ps.resolve_voice(value, self.NAMES)


class LanguageTests(unittest.TestCase):
    def test_every_spelling_of_english_the_pickers_use_is_english(self):
        for value in (None, "", " ", "English", "english", "EN", "en-us", "En-GB", "American English", "british english"):
            self.assertEqual(ps.normalize_language(value), "English", repr(value))

    def test_any_other_language_is_refused_and_the_reason_given(self):
        for value in ("French", "Spanish", "Auto", "zh", 3, ["English"]):
            with self.assertRaisesRegex(ValueError, "speaks English only"):
                ps.normalize_language(value)


class SettingsTests(unittest.TestCase):
    def test_defaults_fill_what_is_left_out(self):
        self.assertEqual(ps.normalize_settings({}), {"seed": 42, "temperature": 0.3, "duration_delta": 0.0})
        self.assertEqual(ps.normalize_settings({"seed": None, "temperature": None}), {"seed": 42, "temperature": 0.3, "duration_delta": 0.0})

    def test_numbers_are_kept_and_whole_numbers_become_the_right_type(self):
        got = ps.normalize_settings({"seed": 7.0, "temperature": 1, "duration_delta": -1 / 2})
        self.assertEqual(got, {"seed": 7, "temperature": 1.0, "duration_delta": -0.5})
        self.assertIsInstance(got["seed"], int)
        self.assertIsInstance(got["temperature"], float)

    def test_the_ends_of_every_range_are_allowed_and_one_step_beyond_is_not(self):
        for key, (low, high) in (("seed", ps.SEED_RANGE), ("temperature", ps.TEMPERATURE_RANGE),
                                 ("duration_delta", ps.DURATION_DELTA_RANGE)):
            for edge in (low, high):
                self.assertEqual(ps.normalize_settings({key: edge})[key], edge)
            for beyond in (low - 1e-6 if key != "seed" else low - 1, high + 1e-6 if key != "seed" else high + 1):
                with self.assertRaisesRegex(ValueError, rf"Phonon {key} must be between"):
                    ps.normalize_settings({key: beyond})

    def test_a_seed_must_be_whole(self):
        with self.assertRaisesRegex(ValueError, "seed must be a whole number"):
            ps.normalize_settings({"seed": 1.5})

    def test_a_setting_that_is_not_a_finite_number_is_refused(self):
        for key in ("seed", "temperature", "duration_delta"):
            for value in ("0.3", True, False, [1], {"x": 1}, math.nan, math.inf, -math.inf):
                with self.assertRaisesRegex(ValueError, rf"Phonon {key} must be a number"):
                    ps.normalize_settings({key: value})


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        Lab(self.folder)

    def test_a_request_becomes_the_voice_language_and_settings_the_worker_needs(self):
        got = ps.check_request({"engine": ps.PHONON_ENGINE_ID, "text": "Hello.", "voice": "zoey", "seed": 5, "duration_delta": 0.2}, self.folder)
        self.assertEqual(got, {"voice": "Zoey", "language": "English", "seed": 5, "temperature": 0.3, "duration_delta": 0.2})

    def test_a_request_with_only_text_speaks_in_the_default_voice(self):
        got = ps.check_request({"engine": ps.PHONON_ENGINE_ID, "text": "Hello."}, self.folder)
        self.assertEqual(got["voice"], "Harper")

    def test_a_parameter_the_engine_does_not_take_is_named(self):
        for extra in ({"speed": 1.2}, {"reference_path": "x.wav"}, {"direction": "softly"}, {"mode": "clone"}, {"top_k": 5, "speed": 1}):
            with self.assertRaisesRegex(ValueError, r"Unsupported Phonon parameter\(s\): " + ", ".join(sorted(extra)) +
                                                    r"\. It takes voice, seed, temperature, duration_delta and language\."):
                ps.check_request({"engine": ps.PHONON_ENGINE_ID, "text": "Hello.", **extra}, self.folder)

    def test_the_request_fields_are_exactly_the_ones_the_check_documents(self):
        self.assertEqual(ps.REQUEST_FIELDS, {"engine", "text", "voice", "language", "seed", "temperature", "duration_delta"})

    def test_a_bad_value_is_refused_before_any_model_is_touched(self):
        for bad in ({"temperature": 2}, {"seed": -1}, {"duration_delta": 1}, {"language": "French"}, {"voice": "Nobody"}):
            with self.assertRaises(ValueError):
                ps.check_request({"engine": ps.PHONON_ENGINE_ID, "text": "Hello.", **bad}, self.folder)


class IdentityOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-phonon-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        Lab(self.folder)
        self.caps = ENTRY["capabilities"]
        self.controls = performance_settings.performance_controls(ps.PHONON_ENGINE_ID, self.caps)

    def options(self, speaker="Zoey", language=None):
        return ps.identity_options(self.caps, speaker, language, self.controls, self.folder)

    def test_an_identity_keeps_the_pins_the_voice_and_what_it_was_chosen_from(self):
        got = self.options("old mortlake")
        self.assertEqual((got["checkpoint"], got["checkpoint_revision"]), (ENTRY["checkpoint"], ENTRY["local_revision"]))
        self.assertEqual((got["voices_checkpoint"], got["voices_revision"]),
                         (ENTRY["backend"]["voices_checkpoint"], ENTRY["backend"]["voices_revision"]))
        self.assertEqual((got["speaker"], got["language"]), ("Old Mortlake", "English"))
        self.assertEqual(got["supported_speakers"], ps.voice_names(self.folder))
        self.assertEqual(got["supported_languages"], ["English"])
        self.assertEqual(got["default_direction"], "")
        self.assertEqual(got["voice_details"]["group"], "Characters")
        self.assertEqual(set(got["voice_details"]), {"group", "gender", "age", "locale", "description"})

    def test_the_settings_the_identity_saw_are_kept_with_it(self):
        got = self.options()
        self.assertEqual([item["id"] for item in got["settings_schema"]], ["seed", "temperature", "duration_delta"])
        self.assertEqual(got["capability_snapshot"]["voice_direction"], {"status": "unsupported"})
        self.assertEqual(got["capability_snapshot"]["sampling_controls"], self.caps["sampling_controls"])
        self.assertEqual(got["capability_snapshot"]["acting_controls"], self.caps["acting_controls"])

    def test_a_voice_has_to_be_chosen_and_installed(self):
        for speaker in (None, "", "  ", 4):
            with self.assertRaisesRegex(ValueError, "Choose a Phonon voice"):
                self.options(speaker)
        with self.assertRaisesRegex(ValueError, "no installed voice named"):
            self.options("Nobody")

    def test_only_english_is_a_language(self):
        self.assertEqual(self.options(language="british english")["language"], "English")
        with self.assertRaisesRegex(ValueError, "English only"):
            self.options(language="German")

    def test_nothing_installed_gives_the_install_instruction(self):
        empty = Path(tempfile.mkdtemp(prefix="voice-lab-phonon-"))
        self.addCleanup(shutil.rmtree, empty, True)
        Lab(empty, records=None)
        with self.assertRaisesRegex(ValueError, "not installed"):
            ps.identity_options(self.caps, "Zoey", None, self.controls, empty)


if __name__ == "__main__":
    unittest.main()
