"""Small libraries for tests: an empty one for a test that starts the service, and a full one for the System panel tests."""
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import numpy as np

import library_backup
from cast_store import CastStore
from evaluation_store import EvaluationStore
from scene_store import SceneStore

RATE = 24_000


def temp_cast(case):
    """An empty Cast store in a temporary library, removed when the test ends. Pass it to serve.make_handler, which keeps the
    Scenes and Comparisons stores in the same library, so the service cannot reach the real one (see real_library_guard.py)."""
    temp = tempfile.TemporaryDirectory(prefix="voice-lab-test-", ignore_cleanup_errors=True)
    case.addCleanup(temp.cleanup)
    return CastStore(Path(temp.name))
PRIVATE_NAME = "Secret villain"
PRIVATE_VOICE = "Hidden voice name"
PRIVATE_LINE = "A private line of dialogue."


def build_library(root, takes=2):
    """Create the library under `root` and return its Cast store. The character, voice and line of dialogue carry names that a
    report or a log must never repeat (PRIVATE_NAME, PRIVATE_VOICE, PRIVATE_LINE)."""
    cast = CastStore(root)
    character = cast.create_character(PRIVATE_NAME, "")
    preset = cast.create_identity(character["id"], "kokoro", PRIVATE_VOICE)
    generation = cast.get_generation_identity(preset["id"])
    audio = (0.1 * np.sin(2 * np.pi * 220 * np.arange(RATE) / RATE)).astype(np.float32)
    for index in range(takes):
        cast.create_take(generation, line_id=f"line-{index}", text=PRIVATE_LINE, spoken_text=PRIVATE_LINE, language="English",
                         direction="", settings={}, model={"backend": "fixture", "checkpoint_revision": "fixture"}, wav=audio,
                         sample_rate=RATE, generation_info={})
    SceneStore(cast, {}, root=root)
    EvaluationStore(root / library_backup.DATABASES["evaluations"], lab_root=root)
    return cast


def use_old_comparison_folder(db_path, out_root):
    """Leave a library the way a build that still called the comparison folder out/m5 left it: the folder under that name, the
    paths in the Comparisons database to match, and the database at schema 2."""
    out_root = Path(out_root)
    (out_root / "comparisons").rename(out_root / "m5")
    first = len("out/comparisons/") + 1
    with closing(sqlite3.connect(db_path)) as db:
        for table, column in (("runs", "manifest_path"), ("items", "raw_path"), ("items", "listening_path")):
            db.execute(f"UPDATE {table} SET {column}='out/m5/' || substr({column}, {first})")
        db.execute("PRAGMA user_version=2")
        db.commit()
