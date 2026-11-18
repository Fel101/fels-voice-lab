"""The local page catalogs and the words the service renders before JavaScript."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import serve
from tests.test_browser_flow import BROWSER, DevToolsPage, FlowServer

from ui_localization import PAGE_TEXT, TOKEN, VALUE, PageText
from ui_preferences import UiPreferences

ROOT = Path(__file__).resolve().parents[1]
CALL = re.compile(r"""\bLabText\.(t|plural)\(\s*['"]([a-z][a-zA-Z0-9_.-]+)['"]""")


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
                    self.assertIsInstance(PAGE_TEXT.english[key], str)
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
            self.assertIn(
                "draft, not yet checked by a speaker",
                page.evaluate("$('languageChoice').selectedOptions[0].textContent"),
            )
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
            self.assertTrue(page.evaluate("document.querySelector('label[for=languageChoice]').innerText.startsWith('Idioma')"))
            self.assertEqual(
                page.evaluate("document.querySelector('h1').textContent"),
                "Compare voices",
            )
            page.set_value("#languageChoice", "en")
            page.wait_until("document.documentElement.lang === 'en' && document.readyState === 'complete'")
            self.assertEqual(UiPreferences(self.root / "library", page_text).get()["language"], "en")
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

    def test_page_markup_text_and_labels_use_the_catalog(self):
        class PageMarkup(HTMLParser):
            in_code = False

            def handle_data(self, data):
                if not self.in_code:
                    word = data.strip()
                    literal = TOKEN.sub("", word).replace("__APP_VERSION__", "").replace("__PAGE_LANGUAGE_OPTIONS__", "")
                    if re.search(r"[a-zA-Z]", literal):
                        self.check(word)

            def handle_starttag(self, tag, attributes):
                if tag in {"style", "script"}:
                    self.in_code = True
                for name, value in attributes:
                    if name in {"title", "aria-label", "placeholder", "alt"} and value:
                        self.check(value)

            def handle_endtag(self, tag):
                if tag in {"style", "script"}:
                    self.in_code = False

        for file in ("serve.html", "compare.html"):
            with self.subTest(file=file):
                parser = PageMarkup()
                parser.check = lambda word: self.assertIsNotNone(TOKEN.fullmatch(word), word)
                parser.feed((ROOT / file).read_text(encoding="utf-8"))
