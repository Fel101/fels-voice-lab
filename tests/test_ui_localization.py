"""The local page catalogs and the words the service renders before JavaScript."""

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import serve
from tests.test_browser_flow import BROWSER, DevToolsPage, FlowServer

from ui_localization import PAGE_TEXT, TOKEN, PageText
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
            json.dumps({"name": name, "draft": draft, "messages": messages}), encoding="utf-8"
        )

    def test_catalog_keys_are_known_and_match_the_kind_of_call(self):
        for source in (*ROOT.glob("*.html"), *ROOT.glob("*.js")):
            body = source.read_text(encoding="utf-8")
            for key in TOKEN.findall(body):
                with self.subTest(source=source.name, key=key):
                    self.assertIsInstance(PAGE_TEXT.english[key], str)
            for call, key in CALL.findall(body):
                with self.subTest(source=source.name, key=key):
                    self.assertIsInstance(PAGE_TEXT.english[key], dict if call == "plural" else str)

    def test_before_javascript_markup_has_the_language_escaped_words_and_english_fallback(self):
        self.save_catalog("es", "Español", {"home.welcome": "<b>Bienvenida</b></script>"}, draft=True)
        store = UiPreferences(self.root, PageText(self.catalogs))
        store.save({"language": "es", "theme": "dark"})
        page = """<html lang="en" data-theme="system"><title>{{ui:home.welcome}}</title>
        <script type="application/json">__PAGE_TEXT_JSON__</script>
        <label>{{ui:language.label}}<select>__PAGE_LANGUAGE_OPTIONS__</select></label>"""
        rendered = store.render(page).decode()
        self.assertIn('<html lang="es" data-theme="dark">', rendered)
        self.assertIn("<title>&lt;b&gt;Bienvenida&lt;/b&gt;&lt;/script&gt;</title>", rendered)
        self.assertIn("<label>Language<select>", rendered)
        self.assertIn('<option value="es" selected>Español (draft, not yet checked by a speaker)</option>', rendered)
        self.assertEqual(rendered.count("</script>"), 1)
        self.assertEqual(store.save({"theme": "forest"})["language"], "es")
        self.assertEqual(store.get()["language"], "es")

    def test_unknown_or_missing_language_reads_as_english_themes_and_updates_are_preserved(self):
        store = UiPreferences(self.root, PageText(self.catalogs))
        store.path.parent.mkdir(parents=True)
        for extra in ({}, {"language": "zz"}, {"language": None}, {"language": ["es"]}):
            store.path.write_text(json.dumps({"theme": "dark", "check_updates": True, **extra}), encoding="utf-8")
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

    @unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
    def test_picker_persists_and_renders_both_pages_with_partial_draft_catalog(self):
        self.save_catalog("es", "Español", {"language.label": "Idioma"}, draft=True)
        page_text = PageText(self.catalogs)
        with mock.patch.object(serve, "UiPreferences", lambda root: UiPreferences(root, page_text)):
            server = FlowServer(self.root / "library")
        page = None
        try:
            page = DevToolsPage(BROWSER)
            page.open(server.base + "/#projects")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "en")
            page.set_value("#languageChoice", "es")
            page.wait_until("document.documentElement.lang === 'es'")
            self.assertEqual(page.evaluate("location.hash"), "#projects")
            self.assertTrue(page.evaluate("document.querySelector('label[for=languageChoice]').innerText.startsWith('Idioma')"))
            self.assertIn(
                "draft, not yet checked by a speaker", page.evaluate("$('languageChoice').selectedOptions[0].textContent")
            )
            page.open(server.base + "/compare")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "es")
            self.assertTrue(page.evaluate("document.querySelector('label[for=languageChoice]').innerText.startsWith('Idioma')"))
            self.assertEqual(page.evaluate("document.querySelector('h1').textContent"), "Compare voices")
            page.set_value("#languageChoice", "en")
            page.wait_until("document.documentElement.lang === 'en'")
            self.assertEqual(UiPreferences(self.root / "library", page_text).get()["language"], "en")
            self.assertEqual(page.problems, [])
        finally:
            if page is not None:
                page.close()
            server.close()
