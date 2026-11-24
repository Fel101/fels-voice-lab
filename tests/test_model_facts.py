"""The facts on each model's card on the Models page (model_facts.py, GET /api/catalog): drawn from the catalog, the adapters,
the installers and the language screen, consistent with MODEL_CAPABILITIES.md's table, and enough to answer the page's three
questions."""

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_lab import engine_adapters  # noqa: E402
from voice_lab import model_facts  # noqa: E402

CATALOG = json.loads((ROOT / "catalog" / "engines.json").read_text(encoding="utf-8"))
# MODEL_CAPABILITIES.md's rows, by the engines each one is about.
TABLE_ROWS = {
    "Kokoro 82M": ["kokoro"],
    "Phonon 31M English": ["phonon-31m-en"],
    "Chatterbox Turbo": ["turbo"],
    "Chatterbox Original": ["orig"],
    "Chatterbox VC": ["chatterbox-vc"],
    "Breeze TTS 2": ["breeze-tts-2"],
    "Qwen 1.7B VoiceDesign": ["qwen3-tts-1.7b-voice-design"],
    "Qwen 1.7B / 0.6B Base": ["qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base"],
    "Qwen 1.7B CustomVoice": ["qwen3-tts-1.7b-custom-voice"],
    "VoxCPM2": ["voxcpm2"],
    "Fish Drama 3": ["drama-3"],
}
NUMBERS = "one two three four five six seven eight nine ten eleven twelve".split()


def all_facts():
    return {entry["id"]: entry["facts"] for entry in model_facts.catalog_with_facts(CATALOG)["engines"]}


def table_rows():
    rows = {}
    for line in (ROOT / "MODEL_CAPABILITIES.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 3 and cells[0] in TABLE_ROWS:
            rows[cells[0]] = cells
    return rows


def fits(facts, cpu=False, clone=False, language=""):
    """The page's filter (applyModelFilter in voice_lab_studio.js), for the questions' answers."""
    return (
        (not cpu or facts["runs"]["cpu"])
        and (not clone or facts["voices"]["clones"])
        and (not language or language in facts["languages"]["filter"])
    )


class CardFactsTests(unittest.TestCase):
    def test_every_catalog_engine_has_a_support_level_and_the_readme_agrees(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        for entry in CATALOG["engines"]:
            with self.subTest(engine=entry["id"]):
                self.assertIn(entry["support"], {"supported", "works", "experimental"})
                self.assertTrue(any(line.startswith(f"| {entry['name']} | {entry['support'].title()} |") for line in readme))
                if entry["support"] == "experimental":
                    self.assertTrue(entry["support_note"].strip())

    def test_every_engine_has_every_fact_in_plain_words(self):
        facts = all_facts()
        self.assertEqual(set(facts), {entry["id"] for entry in CATALOG["engines"]})
        for engine_id, item in facts.items():
            with self.subTest(engine_id):
                for key in ("runs", "voices", "direction", "output"):
                    self.assertTrue(item[key]["text"].endswith("."), item[key]["text"])
                self.assertTrue(item["languages"]["summary"].endswith("."))
                self.assertNotIn("None", json.dumps(item))
                json.dumps(item)  # the API answers it as it is

    def test_the_page_answers_which_model_clones_with_no_graphics_card_in_spanish(self):
        facts = all_facts()
        self.assertEqual(
            [key for key, item in facts.items() if fits(item, cpu=True, clone=True, language="Spanish")], ["drama-3"]
        )
        self.assertEqual([key for key, item in facts.items() if item["first_model"]], ["kokoro"])
        kokoro = facts["kokoro"]
        self.assertTrue(kokoro["runs"]["cpu"] and kokoro["voices"]["presets"] and not kokoro["voices"]["clones"])

    def test_graphics_memory_comes_from_the_installer_rounded_up_to_a_whole_card(self):
        facts = all_facts()
        self.assertEqual(facts["breeze-tts-2"]["runs"]["gpu_memory_gb"], 8)  # tools/install_breeze.py asks for 7.5 GB
        self.assertEqual(facts["voxcpm2"]["runs"]["text"], "Needs a graphics card with at least 7 GB of memory.")
        self.assertEqual(
            facts["qwen3-tts-1.7b-base"]["runs"]["text"], "Needs a graphics card. How much memory it needs is not stated."
        )
        with mock.patch.object(model_facts.importlib, "import_module", side_effect=ImportError("gone")):
            self.assertIsNone(model_facts.gpu_memory_gb(engine_adapters.get("breeze-tts-2")))
        self.assertEqual(facts["turbo"]["runs"]["text"], "Runs with or without a graphics card.")

    def test_what_the_audio_may_be_used_for_says_not_stated_where_nothing_states_it(self):
        facts = all_facts()
        self.assertIn("not stated", facts["phonon-31m-en"]["output"]["text"])
        self.assertIn("37 preset voices", facts["phonon-31m-en"]["output"]["text"])
        self.assertIn("non-commercial", facts["breeze-tts-2"]["output"]["text"])
        self.assertIn("Built with Fish Audio", facts["drama-3"]["output"]["text"])
        for engine_id in ("kokoro", "voxcpm2", "qwen3-tts-1.7b-base"):
            self.assertEqual(
                facts[engine_id]["output"], {"status": "not_stated", "text": "What its audio may be used for: not stated."}
            )
        for engine_id, item in facts.items():
            profile = engine_adapters.get(engine_id).output_license
            if profile["status"] == "restricted":
                self.assertNotIn("not stated", item["output"]["text"], engine_id)

    def test_a_language_the_screen_did_not_understand_is_not_offered_to_the_question(self):
        facts = all_facts()
        base = facts["qwen3-tts-1.7b-base"]["languages"]
        self.assertNotIn("Portuguese", base["filter"])
        self.assertIn("Japanese", base["filter"])  # partly understood
        self.assertIn("partly Japanese", base["summary"])
        drama = facts["drama-3"]["languages"]
        self.assertNotIn("Korean", drama["filter"])
        self.assertNotIn("Hindi", drama["filter"])  # inconclusive, and Drama 3 only notes a line's language
        self.assertNotIn("Welsh", drama["filter"])  # a label it takes that was never screened
        self.assertIn("Never screened: 71 more languages it takes.", drama["text"])
        self.assertEqual(facts["chatterbox-vc"]["languages"]["filter"], [])

    def test_without_the_language_evidence_every_language_is_unscreened(self):
        entry = next(item for item in CATALOG["engines"] if item["id"] == "turbo")
        languages = model_facts.facts(entry, ({}, {}))["languages"]
        self.assertEqual(languages["text"], "English, not yet checked by the Lab's language screen.")
        self.assertEqual(languages["filter"], ["English"])

    def test_the_catalog_is_answered_with_facts_and_left_as_it_was(self):
        before = json.dumps(CATALOG, sort_keys=True)
        answered = model_facts.catalog_with_facts(CATALOG)
        self.assertEqual(json.dumps(CATALOG, sort_keys=True), before)
        self.assertTrue(all("facts" in entry for entry in answered["engines"]))
        self.assertEqual({key for key in answered if key != "engines"}, {key for key in CATALOG if key != "engines"})


class CapabilitiesTableTests(unittest.TestCase):
    """Each card against its row in MODEL_CAPABILITIES.md, so a fact that changes in one place and not the other fails."""

    def test_every_engine_has_a_row(self):
        self.assertEqual(set(table_rows()), set(TABLE_ROWS))
        self.assertEqual(sorted(sum(TABLE_ROWS.values(), [])), sorted(entry["id"] for entry in CATALOG["engines"]))

    def test_cards_agree_with_the_table(self):
        facts, rows = all_facts(), table_rows()
        for model, engine_ids in TABLE_ROWS.items():
            _, abilities, limits = rows[model]
            text = abilities + " " + limits
            for engine_id in engine_ids:
                with self.subTest(engine_id):
                    self.check(engine_id, facts[engine_id], abilities.lower(), text)

    def check(self, engine_id, item, abilities, text):
        voices, runs = item["voices"], item["runs"]
        self.assertEqual(voices["clones"], "clon" in abilities)
        self.assertEqual(voices["designs"], "design" in abilities or "descri" in abilities)
        self.assertEqual(voices["presets"], "preset" in abilities or "fixed voices" in abilities)
        negated = re.search(r"\bno\b[^.;|]*direction", text, re.IGNORECASE)
        named = re.search(r"direct|acting instructions|cues", text, re.IGNORECASE)
        self.assertEqual(item["direction"]["takes"], bool(named) and not negated)
        if engine_adapters.get(engine_id).cpu_only:
            self.assertIn("CPU", text)
        if not runs["cpu"]:
            self.assertIn("card", text)
            self.assertNotIn("CPU", text)
        if runs["gpu_memory_gb"]:
            self.assertIn(f"{runs['gpu_memory_gb']} GB of memory", text)
        elif not runs["cpu"]:
            self.assertNotIn("GB of memory", text)
        if runs["gpu"]:
            self.assertTrue(any(word in text for word in ("--gpu", "GPU build", "AMD's build", "NVIDIA")))
        restricted = item["output"]["status"] == "restricted"
        self.assertEqual(restricted, "non-commercial" in text or "Fish terms" in text)
        self.assertEqual(
            "not stated" in item["output"]["text"] and "37 preset voices" in item["output"]["text"], "no licence" in text
        )
        self.assertEqual("watermark" in item["output"]["text"], "watermark" in text)
        self.check_languages(engine_id, item["languages"], text)

    def check_languages(self, engine_id, languages, text):
        metadata = "metadata" in text
        understood = [row for row in languages["offered"] if row["screen"] == "understood"]
        for row in languages["offered"]:
            if row["screen"] == "understood":
                pass  # named in the row, or counted ("eight of ten picker languages understood"), below
            elif row["screen"] == "partial":
                self.assertTrue(row["language"] in text and "partial" in text, row)
            elif row["screen"] == "not_understood":
                self.assertRegex(text, rf"{row['language']}[^.;]* not understood")
            elif row["screen"] == "inconclusive":
                self.assertRegex(text, rf"{row['language']}[^.;]* inconclusive")
            elif not metadata:
                self.assertIn(row["label"], text)
                self.assertTrue("not screened" in text or "Not in the language screen" in text)
        unscreened = sum(row["screen"] == "not_screened" for row in languages["offered"])
        if metadata:
            self.assertIn(f"{unscreened} labels never screened", text)
        # "eight of ten picker languages understood", or one count per checkpoint where a row covers two.
        size = re.search(r"(\d\.\d)b", engine_id)
        counts = re.findall(rf"(?:(\d\.\dB) understood in )?\b({'|'.join(NUMBERS)}) of (?:ten|twelve)\b", text)
        counts = [word for label, word in counts if not label or (size and label.lower() == size.group(1) + "b")]
        if counts:
            self.assertEqual(NUMBERS.index(counts[0]) + 1, len(understood))
        else:
            for row in understood:
                self.assertIn(row["language"], text)
        for name in re.findall(r"also understood (.+?), which", languages["text"]):
            for language in re.split(r", | and ", name):
                self.assertIn(language, text)
        makers = re.search(r"Its makers list (\d+) languages", languages["text"])
        if makers:
            self.assertIn(f"lists {makers.group(1)} languages", text)


if __name__ == "__main__":
    unittest.main()
