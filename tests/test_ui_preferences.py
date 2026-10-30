"""Appearance persists by state root, without touching models or shared browser storage."""
import json
from pathlib import Path
import tempfile
import unittest

from ui_preferences import UiPreferences


class PreferencesTests(unittest.TestCase):
    def test_default_and_invalid_file_fall_back_without_creating_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = UiPreferences(temp)
            self.assertEqual(store.get(), {"theme": "system"})
            self.assertFalse(store.path.parent.exists())
            store.path.parent.mkdir(parents=True)
            for invalid in ('[1]', '{', '{"theme":"unknown"}', '{"theme":null}'):
                store.path.write_text(invalid, encoding="utf-8")
                self.assertEqual(store.get(), {"theme": "system"})

    def test_save_survives_new_store_and_renders_only_validated_theme(self):
        with tempfile.TemporaryDirectory() as temp:
            store = UiPreferences(temp)
            for theme in UiPreferences.THEMES:
                self.assertEqual(store.save({"theme": theme}), {"theme": theme})
                reopened = UiPreferences(temp)
                self.assertEqual(reopened.get()["theme"], theme)
                self.assertEqual(reopened.render('<html data-theme="system">'),
                                 f'<html data-theme="{theme}">'.encode())
            self.assertEqual(list(store.path.parent.iterdir()), [store.path])

    def test_invalid_payload_preserves_saved_choice_and_other_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = UiPreferences(temp)
            store.save({"theme": "dark"})
            before = store.path.read_bytes()
            for data in (None, [], {}, {"theme": "auto"}, {"theme": {}}, {"theme": "light", "path": "other"}):
                with self.subTest(data=data), self.assertRaises(ValueError):
                    store.save(data)
                self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(json.loads(before), {"theme": "dark"})
            self.assertEqual([p.relative_to(temp).as_posix() for p in Path(temp).rglob('*') if p.is_file()],
                             ['data/ui/preferences.json'])


if __name__ == "__main__":
    unittest.main()
