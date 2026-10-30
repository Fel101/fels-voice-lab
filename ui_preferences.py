"""Small, local appearance preferences, independent of the desktop's HTTP port."""
import json
import os
from pathlib import Path
import tempfile
import threading


class UiPreferences:
    THEMES = ("system", "light", "dark", "forest")

    def __init__(self, root):
        self.path = Path(root) / "data" / "ui" / "preferences.json"
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                theme = data.get("theme") if isinstance(data, dict) else None
                if theme in self.THEMES:
                    return {"theme": theme}
            except (OSError, ValueError):
                pass
            return {"theme": "system"}

    def save(self, data):
        if not isinstance(data, dict) or set(data) != {"theme"} or data["theme"] not in self.THEMES:
            raise ValueError("Choose a theme: system, light, dark, or forest.")
        preference = {"theme": data["theme"]}
        with self._lock:
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
