"""The one-time move of folders that earlier builds named after development milestones."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime_folders
from runtime_folders import FolderMoveError, migrate_program_folders, move_folder


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-folders-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative, text="x"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return path


class MoveFolderTests(FolderTestCase):
    def test_a_folder_takes_its_new_name_with_everything_in_it(self):
        self.write("out/m7/conversions/a/converted.wav", "audio")
        self.assertTrue(move_folder(self.root / "out/m7/conversions", self.root / "out/conversions"))
        self.assertEqual((self.root / "out/conversions/a/converted.wav").read_text("utf-8"), "audio")
        self.assertFalse((self.root / "out/m7/conversions").exists())
        self.assertTrue((self.root / "out/m7").is_dir(), "the old parent is left for its owner to remove")

    def test_a_folder_that_is_not_there_is_not_an_error_and_makes_nothing(self):
        self.assertFalse(move_folder(self.root / "out/m7/conversions", self.root / "out/conversions"))
        self.assertFalse((self.root / "out").exists())

    def test_a_file_with_the_old_name_is_not_a_folder_to_move(self):
        self.write("out/m5", "a file")
        self.assertFalse(move_folder(self.root / "out/m5", self.root / "out/comparisons"))
        self.assertEqual((self.root / "out/m5").read_text("utf-8"), "a file")

    def test_an_empty_folder_at_the_new_name_is_replaced(self):
        self.write("out/m5/run/a.wav")
        (self.root / "out/comparisons").mkdir()
        self.assertTrue(move_folder(self.root / "out/m5", self.root / "out/comparisons"))
        self.assertTrue((self.root / "out/comparisons/run/a.wav").is_file())

    def test_a_folder_with_anything_in_it_at_the_new_name_is_never_merged_or_overwritten(self):
        self.write("out/m5/run-old/a.wav", "old")
        self.write("out/comparisons/run-new/b.wav", "new")
        with self.assertRaisesRegex(FolderMoveError, "already there"):
            move_folder(self.root / "out/m5", self.root / "out/comparisons")
        self.assertEqual((self.root / "out/m5/run-old/a.wav").read_text("utf-8"), "old")
        self.assertEqual((self.root / "out/comparisons/run-new/b.wav").read_text("utf-8"), "new")
        self.assertFalse((self.root / "out/comparisons/run-old").exists())

    def test_a_file_at_the_new_name_is_never_overwritten_either(self):
        self.write("out/m5/run/a.wav")
        self.write("out/comparisons", "a file")
        with self.assertRaises(FolderMoveError):
            move_folder(self.root / "out/m5", self.root / "out/comparisons")
        self.assertEqual((self.root / "out/comparisons").read_text("utf-8"), "a file")
        self.assertTrue((self.root / "out/m5/run/a.wav").is_file())

    def test_a_refusal_by_the_system_is_reported_in_plain_words_and_changes_nothing(self):
        self.write("out/m5/run/a.wav")
        with mock.patch.object(Path, "rename", side_effect=PermissionError("in use")):
            with self.assertRaisesRegex(FolderMoveError, "could not be renamed.*in use"):
                move_folder(self.root / "out/m5", self.root / "out/comparisons")
        self.assertTrue((self.root / "out/m5/run/a.wav").is_file())


class ProgramFolderTests(FolderTestCase):
    def test_each_folder_an_earlier_build_left_is_moved_and_said_so(self):
        self.write("out/m7/conversions/a/converted.wav")
        self.write("out/m6/source/src/voxcpm/__init__.py")
        self.write("out/m11/datasets/set-1/dataset.lock.json")
        self.assertEqual(migrate_program_folders(self.root), [
            "moved out/m7/conversions to out/conversions",
            "moved out/m6/source to out/voxcpm2/source",
            "moved out/m11/datasets to out/datasets"])
        for relative in ("out/conversions/a/converted.wav", "out/voxcpm2/source/src/voxcpm/__init__.py",
                         "out/datasets/set-1/dataset.lock.json"):
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_there_is_nothing_to_say_when_there_is_nothing_to_move_and_a_second_run_is_silent(self):
        self.assertEqual(migrate_program_folders(self.root), [])
        self.write("out/m7/conversions/a.wav")
        self.assertEqual(len(migrate_program_folders(self.root)), 1)
        self.assertEqual(migrate_program_folders(self.root), [])

    def test_one_folder_that_cannot_move_does_not_stop_the_others(self):
        self.write("out/m7/conversions/old.wav")
        self.write("out/conversions/new.wav")
        self.write("out/m11/datasets/set-1/dataset.lock.json")
        notes = migrate_program_folders(self.root)
        self.assertEqual(len(notes), 2)
        self.assertTrue(notes[0].startswith("did not move out/m7/conversions to out/conversions: "))
        self.assertEqual(notes[1], "moved out/m11/datasets to out/datasets")
        self.assertTrue((self.root / "out/m7/conversions/old.wav").is_file())
        self.assertTrue((self.root / "out/conversions/new.wav").is_file())

    def test_worker_logs_left_in_the_old_folders_stay_where_they_are(self):
        log = self.write("out/m1/workers/kokoro/worker-1.log", "log")
        self.assertEqual(migrate_program_folders(self.root), [])
        self.assertTrue(log.is_file())


class NamesMatchTheProgramTests(unittest.TestCase):
    """The new names are written in several places; this fails if one of them is changed without the others."""

    def test_the_folders_the_program_reads_and_writes_are_the_ones_the_move_leaves_them_in(self):
        import chatterbox_vc_worker
        import engine_checks
        import serve
        import voxcpm_worker
        from tools import dataset_workbench
        targets = {new for _old, new in runtime_folders.PROGRAM_MOVES}
        lab = voxcpm_worker.LAB
        self.assertEqual(targets, {"out/conversions", "out/voxcpm2/source", "out/datasets"})
        self.assertIn((serve.OUT / "conversions").resolve(), serve._conversion_roots(target=True))
        self.assertIn((lab / "out" / "conversions").resolve(), chatterbox_vc_worker.ALLOWED_ROOTS)
        self.assertEqual(voxcpm_worker.SOURCE, lab / "out" / "voxcpm2" / "source" / "src")
        self.assertIn("out/voxcpm2/source/src", engine_checks.EXTRA_DIRECTORIES["voxcpm2"])
        self.assertEqual(dataset_workbench.PROJECT_ROOT / "out" / "datasets", lab / "out" / "datasets")


if __name__ == "__main__":
    unittest.main()
