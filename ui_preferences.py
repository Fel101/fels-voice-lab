"""Small, local page preferences, independent of the desktop's HTTP port: the theme, and whether the page checks GitHub for a
newer release once a day when it opens (off until someone switches it on)."""
import json
import os
from pathlib import Path
import tempfile
import threading


class UiPreferences:
    THEMES = ("system", "light", "dark", "forest")
    DEFAULTS = {"theme": "system", "check_updates": False}

    def __init__(self, root):
        self.path = Path(root) / "data" / "ui" / "preferences.json"
        self._lock = threading.Lock()

    def _read(self):
        """The saved preferences, each one valid or its default."""
        preference = dict(self.DEFAULTS)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return preference
        if isinstance(data, dict):
            if data.get("theme") in self.THEMES:
                preference["theme"] = data["theme"]
            if isinstance(data.get("check_updates"), bool):
                preference["check_updates"] = data["check_updates"]
        return preference

    def get(self):
        with self._lock:
            return self._read()

    def save(self, data):
        """Change the preferences named in data (theme, check_updates or both) and keep the others."""
        if not isinstance(data, dict) or not data or not set(data) <= set(self.DEFAULTS):
            raise ValueError("Send theme, check_updates or both.")
        if "theme" in data and data["theme"] not in self.THEMES:
            raise ValueError("Choose a theme: system, light, dark, or forest.")
        if "check_updates" in data and not isinstance(data["check_updates"], bool):
            raise ValueError("check_updates must be true or false.")
        with self._lock:
            preference = {**self._read(), **data}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix="preferences-", suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(json.dumps(preference) + "\n")
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return preference

    def render(self, page):
        return page.replace('data-theme="system"', 'data-theme="' + self.get()["theme"] + '"', 1).encode("utf-8")
