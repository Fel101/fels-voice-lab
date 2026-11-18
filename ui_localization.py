"""Local page words. The service renders static words before the first paint."""

import html
import json
import re
from pathlib import Path

TOKEN = re.compile(r"\{\{ui:([a-z][a-zA-Z0-9_.-]+)\}\}")
VALUE = re.compile(r"\{([a-z][a-zA-Z0-9_]*)\}")
LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
CATEGORIES = {"zero", "one", "two", "few", "many", "other"}


def _places(message):
    forms = message.values() if isinstance(message, dict) else [message]
    return {place for form in forms for place in VALUE.findall(form)}


def _validate_messages(language, messages, english):
    for key, message in messages.items():
        original = english.get(key)
        if original is None or type(original) is not type(message):
            raise ValueError(f"{language}: unknown key or wrong message type: {key}")
        if isinstance(message, dict):
            if not message.keys() <= CATEGORIES or not isinstance(message.get("other"), str):
                raise ValueError(f"{language}: {key} needs an 'other' plural and recognised plural categories")
            if any(not isinstance(form, str) for form in message.values()):
                raise ValueError(f"{language}: {key} has a non-text plural form")
        elif not isinstance(message, str):
            raise ValueError(f"{language}: {key} must be text or plural forms")
        if not _places(message) <= _places(original):
            raise ValueError(f"{language}: {key} has an unknown value place")


class PageText:
    def __init__(self, folder, *, strict=True):
        folder = Path(folder)
        english = self._read_catalog(folder / "en.json")
        self.english = english["messages"]
        _validate_messages("en", self.english, self.english)
        self.catalogs = {"en": english}
        self.skipped_catalogs = []
        for path in sorted(folder.glob("*.json")):
            if path.stem == "en":
                continue
            try:
                catalog = self._read_catalog(path)
                _validate_messages(path.stem, catalog["messages"], self.english)
            except (OSError, ValueError) as error:
                if strict:
                    raise
                detail = " ".join(str(error).splitlines())
                self.skipped_catalogs.append(f"Ignored page translation {path.name}: {detail}")
            else:
                self.catalogs[path.stem] = catalog

    @staticmethod
    def _read_catalog(path):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if not (
            isinstance(catalog, dict)
            and LANGUAGE.fullmatch(path.stem)
            and isinstance(catalog.get("name"), str)
            and catalog["name"].strip()
        ):
            raise ValueError(f"Invalid language in {path.name}")
        if type(catalog.get("draft")) is not bool or not isinstance(catalog.get("messages"), dict):
            raise ValueError(f"{path.name} needs a draft flag and a messages object")
        return catalog

    def language(self, value):
        return value if isinstance(value, str) and value in self.catalogs else "en"

    def render(self, page, language):
        language = self.language(language)
        messages = self.catalogs[language]["messages"]

        def text(key):
            value = messages.get(key, self.english[key])
            if not isinstance(value, str):
                raise ValueError(f"Static page word {key} needs text")
            return value

        def option(code, catalog):
            draft = f" ({text('language.draft')})" if catalog["draft"] else ""
            selected = " selected" if code == language else ""
            return f'<option value="{code}"{selected}>{html.escape(catalog["name"] + draft)}</option>'

        # This is inert JSON. Escaping '<' also prevents a translated line from closing the script element.
        data = json.dumps({"language": language, "english": self.english, "messages": messages if language != "en" else {}})
        options = "".join(option(code, catalog) for code, catalog in self.catalogs.items())
        rendered = page.replace('<html lang="en"', f'<html lang="{language}"', 1)
        rendered = TOKEN.sub(lambda match: html.escape(text(match[1])), rendered)
        replacements = {"__PAGE_TEXT_JSON__": data.replace("<", r"\u003c"), "__PAGE_LANGUAGE_OPTIONS__": options}
        return re.sub(r"__PAGE_(?:TEXT_JSON|LANGUAGE_OPTIONS)__", lambda match: replacements[match[0]], rendered)


PAGE_TEXT = PageText(Path(__file__).resolve().parent / "locales", strict=False)
