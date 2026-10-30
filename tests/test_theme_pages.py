"""The themes are offered and coloured the same way on both pages, System follows Dark, and the text stays readable."""
import re
import unittest
from pathlib import Path

from ui_preferences import UiPreferences

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {"serve.html": {"--bg", "--fg", "--muted", "--card", "--line", "--accent", "--soft", "--err"},
          "compare.html": {"--bg", "--fg", "--muted", "--card", "--line", "--accent", "--err"}}
DARK_THEMES = ("dark", "forest")


def page(name):
    return (ROOT / name).read_text("utf-8")


def block(text, selector):
    """The colours one rule of the page sets, as {"--bg": "#121212", ...}; the rule may be inside a media query."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert found, f"no rule for {selector}"
    rule = found.group(1)
    colours = {name: value for name, value in re.findall(r"(--[a-z]+)\s*:\s*(#[0-9a-fA-F]{3,6})\b", rule)}
    colours["scheme"] = re.search(r"color-scheme\s*:\s*(\w+)", rule).group(1)
    return colours


def themes(name):
    text = page(name)
    return {"light": block(text, ":root"), "dark": block(text, ':root[data-theme="dark"]'),
            "forest": block(text, ':root[data-theme="forest"]'),
            "system": block(text, '@media(prefers-color-scheme:dark){:root[data-theme="system"]')}


def channels(colour):
    digits = colour[1:]
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return [int(digits[index:index + 2], 16) for index in (0, 2, 4)]


def luminance(colour):
    linear = [value / 255 / 12.92 if value / 255 <= 0.03928 else ((value / 255 + 0.055) / 1.055) ** 2.4 for value in channels(colour)]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ThemePagesTests(unittest.TestCase):
    def test_every_theme_is_in_both_menus_and_in_the_script(self):
        for name in TOKENS:
            with self.subTest(page=name):
                menu = re.search(r'<select id="themeChoice">(.*?)</select>', page(name), re.S).group(1)
                self.assertEqual(re.findall(r'<option value="(\w+)">', menu), list(UiPreferences.THEMES))
        listed = re.search(r"\[('system'[^\]]*)\]\.indexOf\(theme\)", page("theme.js")).group(1)
        self.assertEqual(re.findall(r"'(\w+)'", listed), list(UiPreferences.THEMES))

    def test_each_theme_sets_all_the_pages_colours_and_system_is_the_dark_theme(self):
        self.assertEqual(set(UiPreferences.THEMES), {"system", "light", *DARK_THEMES})
        for name, tokens in TOKENS.items():
            found = themes(name)
            for theme, colours in found.items():
                with self.subTest(page=name, theme=theme):
                    self.assertEqual({key for key in colours if key != "scheme"}, tokens)
                    self.assertEqual(colours["scheme"], "light" if theme == "light" else "dark")
            self.assertEqual(found["system"], found["dark"], name)

    def test_dark_is_neutral_grey_and_forest_keeps_the_green(self):
        for name in TOKENS:
            found = themes(name)
            for token in ("--bg", "--card", "--line", "--fg", "--muted"):
                with self.subTest(page=name, token=token):
                    spread = max(channels(found["dark"][token])) - min(channels(found["dark"][token]))
                    self.assertLessEqual(spread, 8, f"{token} is tinted in Dark")
            red, green, _blue = channels(found["forest"]["--bg"])
            self.assertGreater(green, red + 4, name)

    def test_text_stays_readable_in_every_theme(self):
        pairs = [("--fg", "--bg"), ("--fg", "--card"), ("--muted", "--bg"), ("--muted", "--card"), ("--accent", "--card"),
                 ("--err", "--card"), ("--bg", "--accent")]     # the last is the text of a primary button
        for name in TOKENS:
            for theme, colours in themes(name).items():
                for text, ground in pairs:
                    with self.subTest(page=name, theme=theme, text=text, ground=ground):
                        self.assertGreaterEqual(contrast(colours[text], colours[ground]), 4.5)
        for theme, colours in themes("serve.html").items():
            with self.subTest(theme=theme, text="--accent", ground="--soft"):
                self.assertGreaterEqual(contrast(colours["--accent"], colours["--soft"]), 4.5)


if __name__ == "__main__":
    unittest.main()
