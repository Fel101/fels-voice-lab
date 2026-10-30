"""A small Phonon voice catalog on disk, for tests: the English entries of voices.jsonl and an empty file for each voice."""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import phonon_support as ps  # noqa: E402


def record(name, locale="en-us", language="en", filename=None, **more):
    """One line of voices.jsonl as the pinned snapshot has it."""
    character = locale == "characters"
    return {"name": name, "voice_id": name.lower().replace(" ", "-"), "language": language, "locale": locale,
            "gender": None if character else "female", "age": None if character else "Adult", "description": f"{name} speaks.",
            "filename": filename or f"voices/{name.lower().replace(' ', '-')}.safetensors", **more}


VOICES = [record("Zoey"), record("Harper"), record("easton"), record("Alfie", "en-gb"), record("Maeve", "en-gb"),
          record("Old Mortlake", "characters"), record("Azgorath", "characters"),
          record("Amelie", "fr-fr", language="fr")]


class Lab:
    """A lab folder with the real catalog, and the voice-catalog snapshot at the pinned revision filled from `records`."""

    def __init__(self, folder, records=VOICES, files=True):
        self.root = Path(folder)
        (self.root / "catalog").mkdir()
        shutil.copyfile(ROOT / "catalog" / "engines.json", self.root / "catalog" / "engines.json")
        self.model, self.voices = ps.snapshot_dirs(self.root)
        self.voices.mkdir(parents=True)
        (self.voices / "voices").mkdir()
        if records is not None:
            lines = [item if isinstance(item, str) else json.dumps(item) for item in records]
            (self.voices / ps.VOICE_CATALOG).write_text("\n".join(lines) + "\n", "utf-8")
        if files:
            for item in records or []:
                if isinstance(item, dict) and isinstance(item.get("filename"), str):
                    target = self.voices / item["filename"]
                    if target.resolve().is_relative_to(self.voices.resolve()):
                        target.write_bytes(b"x")
