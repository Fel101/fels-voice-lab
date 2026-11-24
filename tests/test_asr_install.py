"""The local installer plans without writing, and installs only pinned checkpoint files."""

from argparse import Namespace
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from voice_lab import asr_installation
from tools import install_asr


class SpeechRecognitionInstallerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / "catalog").mkdir()
        shutil.copyfile(
            asr_installation.LAB / "catalog" / "asr_checkpoints.json",
            self.root / "catalog" / "asr_checkpoints.json",
        )

    def test_dry_run_does_not_touch_the_environment_or_checkpoints(self):
        with (
            mock.patch.object(install_asr.base, "space_problems", return_value=[]),
            mock.patch.object(install_asr.base, "make_env") as make_env,
        ):
            fetch = mock.Mock()
            self.assertEqual(install_asr.run(Namespace(dry_run=True, yes=False), root=self.root, fetch=fetch), 0)
            make_env.assert_not_called()
            fetch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.root / "catalog"])
        self.assertFalse(asr_installation.status(self.root)["ready"])

    def test_install_checks_all_files_against_the_two_pinned_manifests(self):
        python = asr_installation.runtime_python(self.root)
        python.parent.mkdir(parents=True)
        python.touch()
        fetch = mock.Mock()
        with (
            mock.patch.object(install_asr.base, "space_problems", return_value=[]),
            mock.patch.object(install_asr, "check_runtime", return_value=True),
        ):
            self.assertEqual(install_asr.run(Namespace(dry_run=False, yes=True), root=self.root, fetch=fetch), 0)
        models = asr_installation.checkpoints(self.root)
        self.assertEqual(set(models), {"tiny", "small"})
        self.assertEqual(fetch.call_count, 8)
        for spec in models.values():
            self.assertRegex(spec["revision"], r"^[a-f0-9]{40}$")
            folder = asr_installation.model_directory(self.root, spec)
            for item in spec["files"]:
                fetch.assert_any_call(
                    f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{item['path']}",
                    folder / item["path"],
                    item["size"],
                    item["sha256"],
                    say=install_asr.base.say_now,
                )
