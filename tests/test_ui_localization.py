"""The local page catalogs and the words the service renders before JavaScript."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import library_backup
import performance_settings
import serve
from tests.test_browser_flow import BROWSER, DevToolsPage, FlowServer

from ui_localization import PAGE_TEXT, TOKEN, VALUE, PageText
from ui_preferences import UiPreferences

ROOT = Path(__file__).resolve().parents[1]
CALL = re.compile(r"""\bLabText\.(t|plural)\(\s*['"]([a-z][a-zA-Z0-9_.-]+)['"]""")

MODEL_TAG = re.compile(r"\[[^\[\]\r\n]+\]|<\|[^|\r\n]+\|>")
CODE_VALUE = re.compile(
    r"(?:value\d*|toFixed\d*|get\d*|join|slice|size|length\d*|textContent|formatBytes|plural\d*|round|whenText|"
    r"ffmpegMegabytes\d*|bookCount\d*|bookDuration|replaceAll|sceneCueTime|sceneCueRange|sceneTakeTime|sceneCount|"
    r"deviceLabel|elapsedText|utcStamp|installSizeText|referenceQualityText|retirementLabel|takeEditSigned|engineName|"
    r"trim|plainScript|segment\d*|describe|gigabytes2|cacheHit|savedAsset|expectedSequence|gapCount)"
)
JS_TEXT_TOKEN = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`|[()]""")
UI_PROSE = re.compile(r"^[a-z]{2,}$|[A-Za-z]{2,}(?:[ ,.:;!?…]+[A-Za-z])|^[A-Z][a-z]{2,}[. :!?…]*$|[A-Za-z]{3,}[:,] $")
NESTED_KEY = re.compile(r"LabText\.(?:t|plural)\(\s*$")
LITERAL_PLURAL = re.compile(r"""\?\s*(['"])([a-zA-Z][a-zA-Z -]*)\1\s*:\s*(['"])([a-zA-Z][a-zA-Z -]*)\3""")
SHARED_NAMES = {
    "automation.https_provider_example_v1",
    "automation.whisper_1",
    "books.flac",
    "books.m4b",
    "books.mp3",
    "books.wav",
    "cast.16_khz",
    "cast.48_khz",
    "cloning.edinburgh_cstr_vctk",
    "cloning.mozilla_common_voice",
    "compare.fel_s_voice_lab",
    "compare.http",
    "control.min_p",
    "control.top_k",
    "control.top_p",
    "scenes.aac_m4a",
    "scenes.flac",
    "scenes.fountain",
    "scenes.mp3",
    "scenes.ogg",
    "scenes.wav",
    "shell.fel_s_voice_lab",
    "shell.fel_s_voice_lab_2",
    "shell.v",
    "studio.breeze_tts_2",
    "studio.eager",
    "studio.fish_audio_drama_3",
    "studio.phonon_31m_english",
    "studio.qwen3_tts",
    "studio.s",
    "studio.top_k",
    "studio.top_p",
    "studio.voxcpm2",
    "system.ffmpeg",
}


class PageTextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.catalogs = self.root / "locales"
        self.catalogs.mkdir()
        self.save_catalog(
            "en",
            "English",
            {
                **PAGE_TEXT.english,
                "home.welcome": "Welcome",
                "home.greeting": "Hello, {name}",
            },
        )

    def save_catalog(self, code, name, messages, draft=False):
        (self.catalogs / f"{code}.json").write_text(
            json.dumps({"name": name, "draft": draft, "messages": messages}),
            encoding="utf-8",
        )

    def test_catalog_keys_are_known_and_match_the_kind_of_call(self):
        PageText(ROOT / "locales")  # Shipped files must all pass, even though the service can recover.
        for source in (*ROOT.glob("*.html"), *ROOT.glob("*.js")):
            body = source.read_text(encoding="utf-8")
            for key in TOKEN.findall(body):
                with self.subTest(source=source.name, key=key):
                    message = PAGE_TEXT.english[key]
                    self.assertIsInstance(message, str)
                    self.assertIsNone(VALUE.search(message), "HTML tokens have no values to substitute")
            for call, key in CALL.findall(body):
                with self.subTest(source=source.name, key=key):
                    self.assertIsInstance(PAGE_TEXT.english[key], dict if call == "plural" else str)

    def test_before_javascript_markup_has_the_language_escaped_words_and_english_fallback(
        self,
    ):
        self.save_catalog("es", "Español", {"home.welcome": "<b>Bienvenida</b></script>"}, draft=True)
        store = UiPreferences(self.root, PageText(self.catalogs))
        store.save({"language": "es", "theme": "dark"})
        page = """<html lang="en" data-theme="system"><title>{{ui:home.welcome}}</title>
        <script type="application/json">__PAGE_TEXT_JSON__</script>
        <label>{{ui:language.label}}<select>__PAGE_LANGUAGE_OPTIONS__</select></label>"""
        rendered = store.render(page).decode()
        self.assertIn('<html lang="es" data-theme="dark">', rendered)
        self.assertIn("<title>&lt;b&gt;Bienvenida&lt;/b&gt;&lt;/script&gt;</title>", rendered)
        self.assertIn("<label>Page language<select>", rendered)
        self.assertIn(
            '<option value="es" selected>Español (draft, not yet checked by a speaker)</option>',
            rendered,
        )
        self.assertEqual(rendered.count("</script>"), 1)
        self.assertEqual(store.save({"theme": "forest"})["language"], "es")
        self.assertEqual(store.get()["language"], "es")

    def test_unknown_or_missing_language_reads_as_english_themes_and_updates_are_preserved(
        self,
    ):
        store = UiPreferences(self.root, PageText(self.catalogs))
        store.path.parent.mkdir(parents=True)
        for extra in ({}, {"language": "zz"}, {"language": None}, {"language": ["es"]}):
            store.path.write_text(
                json.dumps({"theme": "dark", "check_updates": True, **extra}),
                encoding="utf-8",
            )
            self.assertEqual(store.get(), {"theme": "dark", "check_updates": True, "language": "en"})
        before = store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "page language"):
            store.save({"language": "zz"})
        self.assertEqual(store.path.read_bytes(), before)

    def test_a_translation_cannot_add_unknown_keys_or_value_places(self):
        for messages, problem in (
            ({"does.not.exist": ""}, "unknown key"),
            ({"home.greeting": "Hola, {wrong}"}, "unknown value place"),
            ({"home.greeting": {"other": "Hola, {name}"}}, "wrong message type"),
        ):
            with self.subTest(messages=messages):
                self.save_catalog("es", "Español", messages)
                with self.assertRaisesRegex(ValueError, problem):
                    PageText(self.catalogs)

    def test_service_leaves_out_an_invalid_translation_and_uses_english(self):
        self.save_catalog("es", "Español", {"home.greeting": "Hola, {wrong}"})
        (self.catalogs / "fr.json").write_text("{", encoding="utf-8")
        store_path = self.root / "data" / "ui" / "preferences.json"
        store_path.parent.mkdir(parents=True)
        store_path.write_text('{"language":"es","theme":"forest"}', encoding="utf-8")
        page_text = PageText(self.catalogs, strict=False)
        store = UiPreferences(self.root, page_text)
        self.assertEqual(store.get()["language"], "en")
        self.assertEqual(store.get()["theme"], "forest")
        rendered = store.render('<html lang="en"><select>__PAGE_LANGUAGE_OPTIONS__</select>').decode()
        self.assertIn('<option value="en" selected>English</option>', rendered)
        self.assertNotIn('value="es"', rendered)
        self.assertEqual(len(page_text.skipped_catalogs), 2)
        self.assertIn(
            "es.json: es: home.greeting has an unknown value place",
            page_text.skipped_catalogs[0],
        )
        self.assertIn("fr.json", page_text.skipped_catalogs[1])

        self.save_catalog("en", "English", {"broken": {"one": "One"}})
        with self.assertRaisesRegex(ValueError, "plural"):
            PageText(self.catalogs, strict=False)

    @unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
    def test_picker_persists_and_renders_both_pages_with_partial_draft_catalog(self):
        self.save_catalog(
            "es",
            "Español",
            {
                "language.label": "Idioma",
                "language.draft": "borrador, sin revisar por un hablante",
                "projects.heading": "Proyectos",
                "projects.kind.scene": "Escena",
                "projects.voice_count": {
                    "one": "{count} voz",
                    "other": "{count} voces",
                },
                "projects.line_count": {
                    "one": "{count} línea",
                    "other": "{count} líneas",
                },
            },
            draft=True,
        )
        page_text = PageText(self.catalogs)
        with mock.patch.object(serve, "UiPreferences", lambda root: UiPreferences(root, page_text)):
            server = FlowServer(self.root / "library")
        page = None
        try:
            character = server.cast.create_character("Mira", "")
            voice = server.cast.create_identity(character["id"], "kokoro", "Warm", {"voice": "af_heart"})
            server.cast.update_identity(voice["id"], {"status": "approved"})
            server.scenes.import_script(
                "Mira: Leave the light on.",
                "text",
                {"Mira": {"identity_id": voice["id"]}},
                name="Porch light",
            )
            page = DevToolsPage(BROWSER)
            page.open(server.base + "/#projects")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "en")
            page.set_value("#languageChoice", "es")
            page.wait_until(
                "document.documentElement.lang === 'es' && document.readyState === 'complete' && typeof $ === 'function'"
            )
            self.assertEqual(page.evaluate("location.hash"), "#projects")
            self.assertTrue(page.evaluate("document.querySelector('label[for=languageChoice]').innerText.startsWith('Idioma')"))
            names = ["English", "Español (borrador, sin revisar por un hablante)"]
            self.assertEqual(page.evaluate("Array.from($('languageChoice').options, option => option.textContent)"), names)
            page.wait_until("document.querySelector('#projectRows li')?.textContent.includes('1 voz')")
            self.assertIn(
                "Porch light · Escena · 1 línea · 1 voz · 0 selected takes",
                page.evaluate("document.querySelector('#projectRows li').textContent"),
            )
            self.assertEqual(
                page.evaluate("document.querySelector('#projects h2').textContent"),
                "Proyectos",
            )
            self.assertIn(
                "Search titles or script",
                page.evaluate("$('projectsQuery').labels[0].innerText"),
            )
            page.open(server.base + "/compare")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "es")
            self.assertEqual(page.evaluate("Array.from($('languageChoice').options, option => option.textContent)"), names)
            self.assertTrue(page.evaluate("document.querySelector('label[for=languageChoice]').innerText.startsWith('Idioma')"))
            self.assertEqual(
                page.evaluate("document.querySelector('h1').textContent"),
                "Compare voices",
            )
            page.set_value("#languageChoice", "en")
            page.wait_until("document.documentElement.lang === 'en' && document.readyState === 'complete'")
            self.assertEqual(UiPreferences(self.root / "library", page_text).get()["language"], "en")
            self.assertEqual(page.evaluate("Array.from($('languageChoice').options, option => option.textContent)"), names)
            self.assertEqual(page.problems, [])
        finally:
            if page is not None:
                page.close()
            server.close()

    def test_the_spanish_draft_covers_the_english_catalog_and_value_places(self):
        text = PageText(ROOT / "locales")
        spanish = text.catalogs["es"]
        self.assertEqual(spanish["name"], "Español")
        self.assertTrue(spanish["draft"])
        self.assertEqual(spanish["messages"].keys(), text.english.keys())
        for key, original in text.english.items():
            with self.subTest(key=key):
                translated = spanish["messages"][key]
                if isinstance(original, dict):
                    self.assertEqual(translated.keys(), original.keys())
                    messages = zip(original.values(), (translated[form] for form in original))
                else:
                    messages = [(original, translated)]
                for before, after in messages:
                    self.assertCountEqual(VALUE.findall(before), VALUE.findall(after))
                    self.assertTrue(after.strip())

    def test_translations_preserve_model_tags_and_translate_page_copy(self):
        text = PageText(ROOT / "locales")
        for locale, catalog in text.catalogs.items():
            if locale == "en":
                continue
            for key, message in catalog["messages"].items():
                original = text.english[key]
                if isinstance(message, dict):
                    forms = [(original.get(form, original["other"]), line) for form, line in message.items()]
                else:
                    forms = [(original, message)]
                for before, after in forms:
                    with self.subTest(locale=locale, key=key, text=after):
                        self.assertCountEqual(MODEL_TAG.findall(before), MODEL_TAG.findall(after))
                        if key not in SHARED_NAMES:
                            self.assertNotEqual(before, after)

    def test_catalog_value_names_describe_the_data(self):
        for key, message in PAGE_TEXT.english.items():
            forms = message.values() if isinstance(message, dict) else [message]
            for line in forms:
                with self.subTest(key=key):
                    self.assertFalse([name for name in VALUE.findall(line) if CODE_VALUE.fullmatch(name)])

    def test_page_scripts_pass_data_instead_of_prose_to_messages(self):
        for source in (*ROOT.glob("*.js"), ROOT / "compare.html"):
            body = source.read_text(encoding="utf-8")
            for match in LITERAL_PLURAL.finditer(body):
                singular, plural = match[2], match[4]
                forms = {singular + "s", singular + "es"}
                if singular.endswith("y"):
                    forms.add(singular[:-1] + "ies")
                if plural in forms:
                    line = body.count("\n", 0, match.start()) + 1
                    self.fail(f"{source.name}:{line} chooses raw page plurals: {singular!r}, {plural!r}")
            for call in CALL.finditer(body):
                depth = 1
                for token in JS_TEXT_TOKEN.finditer(body, call.end()):
                    part = token[0]
                    if part == "(":
                        depth += 1
                    elif part == ")":
                        depth -= 1
                        if not depth:
                            break
                    elif not NESTED_KEY.search(body[call.end() : token.start()]):
                        words = part[1:-1]
                        if UI_PROSE.search(words) and not words.startswith(("/api/", "#", ".", "X-")):
                            line = body.count("\n", 0, token.start()) + 1
                            self.fail(f"{source.name}:{line} passes raw page text into a message: {words!r}")

    def test_service_controls_and_backup_sections_have_catalog_labels(self):
        spanish = PAGE_TEXT.catalogs["es"]["messages"]
        for section in library_backup.SECTIONS:
            key = f"system.storage_section.{section.key}"
            with self.subTest(key=key):
                self.assertEqual(PAGE_TEXT.english[key], section.label)
                self.assertIn(key, spanish)
        for controls in performance_settings.CONTROL_UI.values():
            for control, label in controls.items():
                key = f"control.{control}"
                with self.subTest(key=key):
                    self.assertEqual(PAGE_TEXT.english[key], label[0])
                    self.assertIn(key, spanish)

    def test_page_markup_text_and_labels_use_the_catalog(self):
        class PageMarkup(HTMLParser):
            in_code = False
            code_example = False
            fixed_examples = {
                "(laugh)",
                "(sigh)",
                "align_takes.py",
                "out/exports/",
                "restored",
                "tools/dataset_workbench.py",
                "update.bat",
                "update.bat --undo",
            }

            def handle_data(self, data):
                if not self.in_code:
                    word = data.strip()
                    if self.code_example and word in self.fixed_examples:
                        return
                    literal = TOKEN.sub("", word).replace("__APP_VERSION__", "").replace("__PAGE_LANGUAGE_OPTIONS__", "")
                    if re.search(r"[a-zA-Z]", literal):
                        self.check(word)

            def handle_starttag(self, tag, attributes):
                if tag in {"style", "script"}:
                    self.in_code = True
                if tag == "code":
                    self.code_example = True
                for name, value in attributes:
                    if name in {"title", "aria-label", "placeholder", "alt"} and value:
                        if name == "placeholder" and value == "{character}__{line_id}":
                            continue
                        self.check(value)

            def handle_endtag(self, tag):
                if tag in {"style", "script"}:
                    self.in_code = False
                if tag == "code":
                    self.code_example = False

        for file in ("serve.html", "compare.html"):
            with self.subTest(file=file):
                parser = PageMarkup()
                parser.check = lambda word: self.assertIsNotNone(TOKEN.fullmatch(word), word)
                parser.feed((ROOT / file).read_text(encoding="utf-8"))
