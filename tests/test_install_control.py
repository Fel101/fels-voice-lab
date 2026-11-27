"""The phase file a guided installer shares with the service, when Windows refuses to replace it for a moment."""

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from voice_lab import install_control
from voice_lab.install_control import STOPPED, InstallControl


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="voice-lab-install-control-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.control = InstallControl(self.folder)

    def test_a_phase_is_published_once_a_reader_that_holds_the_file_for_half_a_second_lets_go(self):
        self.control.enter("preparing")
        opened = threading.Event()

        def hold():
            with open(self.control.phase_path, encoding="ascii"):  # on Windows this blocks a replace until it closes
                opened.set()
                time.sleep(0.5)

        reader = threading.Thread(target=hold)
        reader.start()
        self.addCleanup(reader.join)
        opened.wait(5)
        self.control.enter("packages")
        self.assertEqual(self.control.current_phase(), "packages")

    def test_a_phase_is_published_after_more_refusals_than_five_quick_tries_allowed(self):
        replace = Path.replace
        refusals = []

        def refuse_then_replace(path, target):
            if len(refusals) < 12:
                refusals.append(path)
                raise PermissionError(5, "Access is denied")
            return replace(path, target)

        with mock.patch.object(Path, "replace", refuse_then_replace), mock.patch.object(install_control, "_pause"):
            self.control.enter("packages")
        self.assertEqual(len(refusals), 12)
        self.assertEqual(self.control.current_phase(), "packages")

    def test_a_file_that_stays_locked_is_reported_after_the_busy_time(self):
        def refuse(path, target):
            raise PermissionError(5, "Access is denied")

        with mock.patch.object(Path, "replace", refuse), mock.patch.object(install_control, "BUSY_SECONDS", 0.2):
            started = time.monotonic()
            with self.assertRaises(PermissionError):
                self.control.enter("packages")
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_the_waits_grow_to_a_quarter_second_and_are_not_all_the_same_length(self):
        with mock.patch.object(install_control.time, "sleep") as sleep, \
                mock.patch.object(install_control.random, "uniform", return_value=1.0):
            for attempt in range(7):
                install_control._pause(attempt)
        self.assertEqual([round(call.args[0], 3) for call in sleep.call_args_list],
                         [0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25])
        with mock.patch.object(install_control.time, "sleep") as sleep:
            for _ in range(20):
                install_control._pause(5)
        self.assertGreater(len({call.args[0] for call in sleep.call_args_list}), 1)

    def test_stop_is_still_honoured_after_a_refused_replace(self):
        replace = Path.replace
        refused = []

        def refuse_once(path, target):
            if not refused:
                refused.append(path)
                raise PermissionError(5, "Access is denied")
            return replace(path, target)

        self.control.stop_path.touch()
        with mock.patch.object(Path, "replace", refuse_once), mock.patch.object(install_control, "_pause"):
            with self.assertRaises(SystemExit) as stopped:
                self.control.enter("packages")
        self.assertEqual(stopped.exception.code, STOPPED)
        self.assertEqual(self.control.current_phase(), "packages")


if __name__ == "__main__":
    unittest.main()
