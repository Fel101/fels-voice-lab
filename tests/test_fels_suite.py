"""fels_suite.py, the file every app of Fel's Studio shares (contract 1): its shared test cases, and what they cannot cover,
such as a lock held by another process and folders that cannot be made."""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_lab import fels_suite  # noqa: E402

VECTORS = Path(__file__).resolve().parent / "suite_vectors.json"


class SharedCaseTests(unittest.TestCase):
    def test_the_shared_test_cases_pass(self):
        self.assertEqual(fels_suite.check_vectors(VECTORS), [])

    def test_the_cases_are_for_this_version_of_the_file(self):
        vectors = json.loads(VECTORS.read_text("utf-8"))
        self.assertEqual((vectors["suite_version"], vectors["contract"]), (fels_suite.FELS_SUITE_VERSION, fels_suite.CONTRACT))

    def test_the_file_reads_on_python_3_8_and_imports_only_the_standard_library(self):
        source = (ROOT / "voice_lab/fels_suite.py").read_text("utf-8")
        tree = ast.parse(source, feature_version=(3, 8))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {item.name.split(".")[0] for item in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.add(node.module.split(".")[0])
        self.assertEqual(sorted(names - set(sys.stdlib_module_names)), [])
        for later in ("removeprefix", "removesuffix", "is_relative_to", "ignore_cleanup_errors"):
            self.assertNotIn(later, source, f"{later} is newer than Python 3.8")


class SuiteFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.default = {"LOCALAPPDATA": str(self.temp / "local"), "XDG_STATE_HOME": str(self.temp / "state")}

    def test_fels_suite_dir_moves_it_and_it_is_made(self):
        environ = {**self.default, "FELS_SUITE_DIR": str(self.temp / "D" / "Fels")}
        self.assertEqual(fels_suite.suite_dir(environ), self.temp / "D" / "Fels")
        self.assertTrue((self.temp / "D" / "Fels").is_dir())
        self.assertIsNone(fels_suite.suite_dir_problem(environ))

    def test_reading_never_makes_the_default_folder(self):
        environ = dict(self.default)
        self.assertIsNone(fels_suite.read_address("voice-lab", environ))
        self.assertEqual(fels_suite.gpu_status(environ), {"holders": [], "alone": None})
        self.assertEqual(fels_suite.inbox_list("voice-lab", [".voiceslate"], 100, environ), [])
        self.assertIsNone(fels_suite.find_app("voice-lab", lambda url: None, environ=environ)["url"])
        self.assertFalse((self.temp / "local").exists() or (self.temp / "state").exists())
        fels_suite.inbox_dir("voice-lab", environ, make=True)
        self.assertTrue(fels_suite.inbox_dir("voice-lab", environ).is_dir(), "a receiver makes it")

    def test_a_folder_that_cannot_be_made_falls_back_to_the_default_and_says_so(self):
        blocker = self.temp / "a-file"
        blocker.write_text("x", "utf-8")
        for given in (str(blocker / "Fels"), "relative\\Fels" if os.name == "nt" else "relative/Fels"):
            with self.subTest(given):
                environ = {**self.default, "FELS_SUITE_DIR": given}
                default = Path(fels_suite.default_suite_dir(environ=environ))
                self.assertEqual(fels_suite.suite_dir(environ), default)
                problem = fels_suite.suite_dir_problem(environ)
                self.assertIn("FELS_SUITE_DIR", problem)
                self.assertIn(str(default), problem)

    @unittest.skipUnless(os.name == "nt", "drive letters")
    def test_a_drive_that_is_gone_falls_back_to_the_default(self):
        used = {Path(f"{letter}:\\").exists() and letter for letter in "BEFGHIJKLMNOPQRSTUVWXYZ"}
        missing = next(letter for letter in "ZYXWVUTSRQPONMLKJIHGFE" if letter not in used)
        environ = {**self.default, "FELS_SUITE_DIR": f"{missing}:\\Fels"}
        self.assertEqual(fels_suite.suite_dir(environ), self.temp / "local" / "Fels")
        self.assertIn("cannot be made", fels_suite.suite_dir_problem(environ))


class AddressFileTests(unittest.TestCase):
    def setUp(self):
        self.environ = {"FELS_SUITE_DIR": self.enterContext(tempfile.TemporaryDirectory())}
        self.living = {os.getpid(), 4242}

    def alive(self, pid):
        return pid in self.living

    def test_a_second_copy_works_but_is_not_announced_and_cannot_remove_the_first(self):
        self.assertTrue(
            fels_suite.write_address(
                "voice-lab", "http://127.0.0.1:8765", "1.0.0", 1, "a", self.environ, pid=4242, alive=self.alive
            )
        )
        self.assertFalse(
            fels_suite.write_address("voice-lab", "http://127.0.0.1:50001", "1.0.0", 1, "b", self.environ, alive=self.alive)
        )
        self.assertFalse(fels_suite.remove_address("voice-lab", self.environ))
        entry = fels_suite.read_address("voice-lab", self.environ, alive=self.alive)
        self.assertEqual((entry["url"], entry["name"], entry["contract"]), ("http://127.0.0.1:8765", "Fel's Voice Lab", 1))
        self.living.discard(4242)
        self.assertIsNone(fels_suite.read_address("voice-lab", self.environ, alive=self.alive), "a stale file is ignored")
        self.assertTrue(
            fels_suite.write_address("voice-lab", "http://127.0.0.1:50001", "1.0.0", 1, "b", self.environ, alive=self.alive),
            "and replaced by the next copy",
        )
        self.assertTrue(fels_suite.remove_address("voice-lab", self.environ))
        self.assertFalse(fels_suite.address_path("voice-lab", self.environ).exists())
        self.assertEqual(os.listdir(fels_suite.suite_dir(self.environ) / "apps"), [], "no temporary file is left")


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.environ = {"FELS_SUITE_DIR": self.enterContext(tempfile.TemporaryDirectory())}

    def test_a_sender_needs_the_receivers_inbox_and_a_kind_it_takes(self):
        with self.assertRaises(FileNotFoundError):
            fels_suite.inbox_write("voice-lab", "voice-slate", "take.voiceslate", b"PK", self.environ)
        fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        with self.assertRaises(ValueError):
            fels_suite.inbox_write("voice-lab", "voice-slate", "take.wav", b"RIFF", self.environ)
        with self.assertRaises(ValueError):
            fels_suite.inbox_write("npc-forge", "voice-slate", "take.voiceslate", b"PK", self.environ)

    def test_a_handoff_arrives_whole_and_two_of_one_name_both_arrive(self):
        inbox = fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        first = fels_suite.inbox_write("voice-lab", "voice-slate", "Maya.voiceslate", b"one", self.environ)
        second = fels_suite.inbox_write("voice-lab", "voice-slate", "Maya.voiceslate", b"two", self.environ)
        self.assertNotEqual(first, second)
        self.assertEqual({first.read_bytes(), second.read_bytes()}, {b"one", b"two"})
        self.assertEqual(
            sorted(path.name for path in inbox.iterdir()), sorted([first.name, second.name]), "nothing half-written"
        )
        listed = fels_suite.inbox_list("voice-lab", [".voiceslate"], 32 * 1048576, self.environ)
        self.assertEqual([item["sender_name"] for item in listed], ["Voice Slate", "Voice Slate"])
        self.assertEqual([item["problem"] for item in listed], [None, None])

    def test_a_file_too_large_or_of_another_kind_says_why(self):
        inbox = fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        (inbox / "big.voiceslate").write_bytes(b"x" * 3 * 1048576)
        (inbox / "song.mp3").write_bytes(b"x")
        listed = {
            item["name"]: item["problem"]
            for item in fels_suite.inbox_list("voice-lab", [".voiceslate"], 2 * 1048576, self.environ)
        }
        self.assertEqual(listed["big.voiceslate"], "It is 3.0 MB; Voice Lab takes files up to 2 MB.")
        self.assertEqual(listed["song.mp3"], "Voice Lab does not take .mp3 files.")

    def test_only_a_file_waiting_in_an_inbox_is_cleared(self):
        inbox = fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        waiting = fels_suite.inbox_write("voice-lab", "voice-slate", "a.voiceslate", b"x", self.environ)
        cleared = []
        outside = Path(self.environ["FELS_SUITE_DIR"]) / "apps" / "voice-lab.json"
        outside.parent.mkdir(parents=True)
        outside.write_text("{}", "utf-8")
        for wrong in (outside, inbox / "missing.voiceslate", Path(self.environ["FELS_SUITE_DIR"]) / "inbox" / "x.voiceslate"):
            with self.subTest(str(wrong)), self.assertRaises(ValueError):
                fels_suite.inbox_done(wrong, self.environ, discard=cleared.append)
        fels_suite.inbox_done(waiting, self.environ, discard=cleared.append)
        self.assertEqual(cleared, [str(waiting)])

    @unittest.skipIf(os.name == "nt", "on Windows the file goes to the Recycle Bin, which a test should not fill")
    def test_elsewhere_a_cleared_file_is_deleted(self):
        fels_suite.inbox_dir("voice-lab", self.environ, make=True)
        waiting = fels_suite.inbox_write("voice-lab", "voice-slate", "a.voiceslate", b"x", self.environ)
        fels_suite.inbox_done(waiting, self.environ)
        self.assertFalse(waiting.exists())


HOLD = """
import os
import sys
sys.path.insert(0, sys.argv[1])
from voice_lab import fels_suite
lock = fels_suite.GpuLock("npc-forge", {"FELS_SUITE_DIR": sys.argv[2]})
print(lock.acquire("pictures", alone=sys.argv[3] == "alone", timeout=10), os.getpid(), flush=True)
sys.stdin.readline()
lock.release()
"""


class GpuLockTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.environ = {"FELS_SUITE_DIR": self.folder}

    def hold_in_another_process(self, how):
        child = subprocess.Popen(
            [sys.executable, "-c", HOLD, str(ROOT), self.folder, how], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(child.wait, 30)
        self.addCleanup(child.stdin.close)
        held, pid = child.stdout.readline().split()
        self.assertEqual(held, "True")
        child.holder_pid = int(pid)  # a virtual environment's python.exe starts the real one: its pid is the holder's
        return child

    def test_another_process_holding_the_card_alone_keeps_this_one_out_until_it_lets_go(self):
        child = self.hold_in_another_process("alone")
        lock = fels_suite.GpuLock("voice-lab", self.environ)
        self.assertFalse(lock.acquire("Kokoro 82M", timeout=0.3, poll=0.05))
        [holder] = lock.holders()
        self.assertEqual((holder["app"], holder["pid"], holder["alone"]), ("npc-forge", child.holder_pid, True))
        self.assertEqual(lock.alone_wanted()["app"], "npc-forge")
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertTrue(lock.acquire("Kokoro 82M", timeout=20, poll=0.05))
        self.assertIsNone(lock.alone_wanted(), "alone.json went with the lock")
        self.assertEqual([entry["app"] for entry in lock.holders()], ["voice-lab"])
        lock.release()
        self.assertEqual(lock.holders(), [])

    def test_a_shared_holder_in_another_process_shares_and_keeps_an_alone_take_waiting(self):
        self.hold_in_another_process("shared")
        lab = fels_suite.GpuLock("voice-lab", self.environ)
        self.assertTrue(lab.acquire("Kokoro 82M", timeout=0))
        lab.release()
        forge = fels_suite.GpuLock("npc-forge", self.environ, pid=os.getpid() + 1, alive=lambda pid: True)
        self.assertFalse(forge.acquire("pictures", alone=True, timeout=0.3, poll=0.05))
        self.assertIsNone(fels_suite.alone_wanted(self.environ), "giving up takes the want back")

    def test_a_wait_can_be_cancelled(self):
        self.hold_in_another_process("alone")
        lock = fels_suite.GpuLock("altera", self.environ)
        calls = []
        self.assertFalse(lock.acquire("a chat reply", cancel=lambda: calls.append(1) or len(calls) > 2, poll=0.01))
        self.assertEqual(len(calls), 3)

    def test_taking_it_again_says_what_is_loaded_now(self):
        lock = fels_suite.GpuLock("voice-lab", self.environ)
        self.assertTrue(lock.acquire("Kokoro 82M", timeout=0))
        self.assertTrue(lock.acquire("Qwen3-TTS", timeout=0))
        self.assertEqual(lock.holders()[0]["what"], "Qwen3-TTS")
        with self.assertRaises(ValueError):
            lock.acquire("pictures", alone=True, timeout=0)
        lock.release()

    def test_a_holder_is_described_in_one_sentence(self):
        shared = {"app": "voice-lab", "what": "Kokoro 82M", "since": "2026-12-01T10:02:11Z", "alone": False}
        self.assertRegex(fels_suite.holder_sentence(shared), r"^Voice Lab is using the GPU for Kokoro 82M since \d\d:\d\d\.$")
        alone = {"app": "npc-forge", "what": "pictures", "since": "2026-12-01T10:02:11Z"}
        self.assertRegex(
            fels_suite.holder_sentence(alone),
            r"^NPC Forge is using the whole GPU for pictures \(One model at a time\) since \d\d:\d\d\.$",
        )


if __name__ == "__main__":
    unittest.main()
