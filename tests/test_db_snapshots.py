"""A database about to move to a newer schema is copied first, and the newest few copies are kept."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cast_store import CAST_SCHEMA_VERSION, CastStore
from db_snapshots import FOLDER, KEEP, SnapshotError, snapshot_before_migration
from evaluation_store import SCHEMA_VERSION as EVALUATION_SCHEMA_VERSION, EvaluationStore
from scene_store import SceneStore


def user_version(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()


class SnapshotHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-snapshots-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "thing.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        self.db.executemany("INSERT INTO notes (body) VALUES (?)", [("one",), ("two",)])
        self.db.commit()

    def folder(self):
        return self.path.parent / FOLDER

    def test_a_new_or_current_database_has_nothing_to_lose_and_is_not_copied(self):
        for version in (0, 5):
            self.assertIsNone(snapshot_before_migration(self.db, self.path, version, 5, "thing"))
        self.assertFalse(self.folder().exists())

    def test_an_older_database_is_copied_with_its_rows_and_its_old_version(self):
        self.db.execute("PRAGMA user_version=3")
        copy = snapshot_before_migration(self.db, self.path, 3, 5, "thing")
        self.assertEqual(copy.parent, self.folder())
        self.assertRegex(copy.name, r"thing-schema3-\d{8}-\d{6}-\d+\.sqlite3\Z")
        self.assertEqual(user_version(copy), 3)
        check = sqlite3.connect(copy)
        try:
            self.assertEqual([row[0] for row in check.execute("SELECT body FROM notes ORDER BY id")], ["one", "two"])
        finally:
            check.close()

    def test_only_the_newest_few_copies_are_kept(self):
        self.db.execute("PRAGMA user_version=2")
        made = []
        for number in range(KEEP + 2):
            # Copies made in the same second share a name, so give each its own before the next one is made.
            copy = snapshot_before_migration(self.db, self.path, 2, 5, "thing")
            kept = copy.with_name(f"thing-schema2-2026010{number + 1}-000000-1.sqlite3")
            copy.rename(kept)
            os.utime(kept, (1_700_000_000 + number, 1_700_000_000 + number))
            made.append(kept)
        remaining = sorted(self.folder().iterdir(), key=lambda item: item.stat().st_mtime)
        self.assertEqual(remaining, made[-KEEP:])

    def test_other_databases_copies_are_left_alone_by_the_pruning(self):
        self.db.execute("PRAGMA user_version=2")
        other = self.folder() / "other-schema1-20260101-000000-1.sqlite3"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"not mine")
        for _ in range(KEEP + 2):
            snapshot_before_migration(self.db, self.path, 2, 5, "thing")
        self.assertTrue(other.exists())

    def test_when_the_copy_cannot_be_made_the_caller_is_told_to_stop(self):
        self.db.execute("PRAGMA user_version=2")
        self.folder().write_bytes(b"a file where the folder should be")
        with self.assertRaisesRegex(SnapshotError, "before updating it to schema 5.*Nothing was changed"):
            snapshot_before_migration(self.db, self.path, 2, 5, "thing")


class StoresSnapshotBeforeTheyMigrateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-snapshot-stores-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_cast_is_copied_at_its_old_schema_then_migrated_and_not_copied_again(self):
        store = CastStore(self.root)
        character = store.create_character("Mira", "Warm")
        folder = store.data_root / FOLDER
        self.assertFalse(folder.exists())  # a new database and a current one are never copied
        db = sqlite3.connect(store.db_path)
        try:
            db.execute("ALTER TABLE takes DROP COLUMN waveform_checks_json")
            db.execute("ALTER TABLE takes DROP COLUMN word_timings_json")
            db.execute("ALTER TABLE takes DROP COLUMN phoneme_timings_json")
            db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
            db.execute("DROP INDEX takes_parent")
            db.execute("ALTER TABLE takes DROP COLUMN edit_json")
            db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
            db.execute("DROP TABLE purged_items")
            db.execute("DROP TABLE purge_batches")
            db.execute("DROP TABLE retirements")
            db.execute("PRAGMA user_version=3")
            db.commit()
        finally:
            db.close()
        reopened = CastStore(self.root)
        self.assertEqual(reopened.get_character(character["id"])["name"], "Mira")
        self.assertEqual(user_version(store.db_path), CAST_SCHEMA_VERSION)
        copies = list(folder.iterdir())
        self.assertEqual(len(copies), 1)
        self.assertRegex(copies[0].name, r"cast-schema3-")
        self.assertEqual(user_version(copies[0]), 3)
        check = sqlite3.connect(copies[0])
        try:
            self.assertEqual(check.execute("SELECT name FROM characters").fetchall(), [("Mira",)])
        finally:
            check.close()
        CastStore(self.root).list_cast()
        self.assertEqual(len(list(folder.iterdir())), 1)

    def test_a_cast_database_that_cannot_be_copied_is_not_migrated(self):
        store = CastStore(self.root)
        store.create_character("Mira", "Warm")
        db = sqlite3.connect(store.db_path)
        try:
            db.execute("PRAGMA user_version=6")
            db.execute("ALTER TABLE takes DROP COLUMN phoneme_timings_json")
            db.execute("ALTER TABLE takes DROP COLUMN loudness_json")
            db.execute("DROP INDEX takes_parent")
            db.execute("ALTER TABLE takes DROP COLUMN edit_json")
            db.execute("ALTER TABLE takes DROP COLUMN parent_take_id")
            db.commit()
        finally:
            db.close()
        (store.data_root / FOLDER).write_bytes(b"in the way")
        with self.assertRaises(SnapshotError):
            CastStore(self.root).list_cast()
        self.assertEqual(user_version(store.db_path), 6)

    def test_evaluations_are_copied_at_their_old_schema(self):
        path = self.root / "data" / "evaluations" / "evaluations.sqlite3"
        EvaluationStore(path, lab_root=self.root)
        db = sqlite3.connect(path)
        try:
            db.execute("DROP TABLE side_ratings")
            db.execute("PRAGMA user_version=1")
            db.commit()
        finally:
            db.close()
        EvaluationStore(path, lab_root=self.root)
        self.assertEqual(user_version(path), EVALUATION_SCHEMA_VERSION)
        copies = list((path.parent / FOLDER).iterdir())
        self.assertEqual(len(copies), 1)
        self.assertEqual(user_version(copies[0]), 1)

    def test_a_current_scene_database_is_never_copied(self):
        cast = CastStore(self.root)
        SceneStore(cast, {}, root=self.root)
        SceneStore(cast, {}, root=self.root)
        self.assertFalse((self.root / "data" / "scenes" / FOLDER).exists())


if __name__ == "__main__":
    unittest.main()
