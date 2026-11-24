"""lab_address.py: the known address, the address file a running service writes for other programs, the window's port, and
how the service binds its port (serve.bind_service)."""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_lab import fels_suite  # noqa: E402
from voice_lab import lab_address  # noqa: E402
from voice_lab import serve  # noqa: E402


class AddressFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.folder / "service.json"
        self.living = {os.getpid()}

    def alive(self, pid):
        return pid in self.living

    def write(self, url, root="C:\\Lab"):
        return lab_address.write(url, version="0.4.0", root=root, path=self.path, alive=self.alive)

    def test_the_file_is_per_user_outside_any_lab_and_a_test_can_move_it(self):
        self.assertEqual(
            lab_address.address_file({"LOCALAPPDATA": r"D:\Profiles\AppData\Local"}),
            Path(r"D:\Profiles\AppData\Local") / "FelsVoiceLabService" / "service.json",
        )
        self.assertEqual(lab_address.address_file({"VOICE_LAB_ADDRESS_FILE": str(self.path)}), self.path)
        self.assertNotEqual(lab_address.FOLDER_NAME, "FelsVoiceLab", "Setup.exe installs the Lab into FelsVoiceLab")

    def test_a_service_writes_its_address_and_removes_it_when_it_stops(self):
        self.assertTrue(self.write("http://127.0.0.1:8765"))
        entry = lab_address.read(self.path, alive=self.alive)
        self.assertEqual(
            {key: entry[key] for key in ("url", "version", "pid", "root")},
            {"url": "http://127.0.0.1:8765", "version": "0.4.0", "pid": os.getpid(), "root": "C:\\Lab"},
        )
        self.assertEqual(list(self.folder.iterdir()), [self.path], "no temporary file is left")
        self.assertTrue(lab_address.remove(path=self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse(lab_address.remove(path=self.path))

    def test_a_file_left_by_a_process_that_has_gone_is_ignored_and_replaced(self):
        self.path.write_text(json.dumps({"schema": 1, "url": "http://127.0.0.1:50001", "pid": 99999}), encoding="utf-8")
        self.assertIsNone(lab_address.read(self.path, alive=self.alive))
        self.assertTrue(self.write("http://127.0.0.1:8765"))
        self.assertEqual(lab_address.read(self.path, alive=self.alive)["url"], "http://127.0.0.1:8765")

    def test_a_second_service_leaves_the_first_ones_address_alone(self):
        self.living.add(4242)
        self.path.write_text(json.dumps({"schema": 1, "url": "http://127.0.0.1:8765", "pid": 4242}), encoding="utf-8")
        self.assertFalse(self.write("http://127.0.0.1:50002", root="C:\\Other"))
        self.assertFalse(lab_address.remove(path=self.path))
        self.assertEqual(lab_address.read(self.path, alive=self.alive)["url"], "http://127.0.0.1:8765")

    def test_files_that_are_not_an_address_are_ignored(self):
        for text in ("", "not json", "[]", '{"schema": 2, "url": "x", "pid": 1}', '{"schema": 1, "pid": 1}'):
            self.path.write_text(text, encoding="utf-8")
            self.assertIsNone(lab_address.read(self.path, alive=lambda pid: True), text)

    def test_the_lab_is_looked_for_in_order(self):
        suite = {"FELS_SUITE_DIR": str(self.folder / "suite")}

        def find(given=None, environ=None):
            return lab_address.find_url(given, {**suite, **(environ or {})}, path=self.path, alive=self.alive)

        self.assertEqual(find(), ("http://127.0.0.1:8765", "the known address"))
        self.write("http://127.0.0.1:50003")
        self.assertEqual(find(), ("http://127.0.0.1:50003", "the address file"))
        fels_suite.write_address("voice-lab", "http://127.0.0.1:50004", "1.0.0", 1, "a", suite, alive=self.alive)
        self.assertEqual(find(), ("http://127.0.0.1:50004", "the suite's address file"))
        self.assertEqual(find(environ={"VOICE_LAB_URL": "http://127.0.0.1:9000"}), ("http://127.0.0.1:9000", "VOICE_LAB_URL"))
        self.assertEqual(
            find("http://127.0.0.1:9001", {"VOICE_LAB_URL": "http://127.0.0.1:9000"}), ("http://127.0.0.1:9001", "--url")
        )

    def test_this_process_is_alive_and_a_made_up_one_is_not(self):
        self.assertTrue(lab_address.process_alive(os.getpid()))
        self.assertFalse(lab_address.process_alive(0))
        self.assertFalse(lab_address.process_alive("12"))
        self.assertFalse(lab_address.process_alive(True))


class LabIdTests(unittest.TestCase):
    def test_one_folder_and_library_have_one_id_and_another_library_another(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "library").mkdir()
        same = lab_address.lab_id(folder, folder)
        self.assertEqual(same, lab_address.lab_id(str(folder) + os.sep, folder / "."))
        self.assertNotEqual(same, lab_address.lab_id(folder, folder / "library"))
        self.assertEqual(len(same), 16)


class WindowPortTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = self.root / lab_address.SETTINGS

    def test_the_known_port_unless_the_settings_name_another(self):
        self.assertEqual(lab_address.window_port(self.root), 8765)
        for text, port in (('{"port": 0}', 0), ('{"port": 9123}', 9123), ('{"port": 80}', 8765), ('{"port": "9"}', 8765),
                           ('{"port": true}', 8765), ("not json", 8765), ("[]", 8765)):  # fmt: skip
            self.settings.write_text(text, encoding="utf-8")
            self.assertEqual(lab_address.window_port(self.root), port, text)

    def test_saving_keeps_the_other_settings_and_the_default_is_not_written(self):
        self.settings.write_text('{"state_root": "D:\\\\library"}', encoding="utf-8")
        self.assertEqual(lab_address.save_window_port(self.root, 0), 0)
        self.assertEqual(json.loads(self.settings.read_text("utf-8")), {"state_root": "D:\\library", "port": 0})
        lab_address.save_window_port(self.root, 8765)
        self.assertEqual(json.loads(self.settings.read_text("utf-8")), {"state_root": "D:\\library"})
        for bad in (80, 70000, "8765", None, True):
            with self.assertRaisesRegex(ValueError, "0, for any free port"):
                lab_address.save_window_port(self.root, bad)
        self.settings.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            lab_address.save_window_port(self.root, 9000)
        self.assertEqual(self.settings.read_text("utf-8"), "not json")


class Answering(BaseHTTPRequestHandler):
    answer = b'{"status": "ready"}'

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.answer)))
        self.end_headers()
        self.wfile.write(self.answer)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HealthAndPortTests(unittest.TestCase):
    def serve(self, answer):
        handler = type("Handler", (Answering,), {"answer": answer})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_health_is_read_and_a_lab_is_told_from_another_program(self):
        port = self.serve(b'{"status": "ready", "app": "Fel\'s Voice Lab", "version": "0.4.0", "lab_id": "abc"}')
        answer = lab_address.ask_health(f"http://127.0.0.1:{port}/")
        self.assertTrue(lab_address.is_lab(answer))
        self.assertTrue(lab_address.is_lab({"version": "0.3.0", "status": "ready", "workers": []}), "0.3.0 had no app")
        self.assertFalse(lab_address.is_lab({"status": "ok"}))
        self.assertIsNone(lab_address.ask_health(f"http://127.0.0.1:{self.serve(b'not json')}"))
        self.assertIsNone(lab_address.ask_health(f"http://127.0.0.1:{free_port()}", timeout=1))
        self.assertEqual(
            lab_address.moved_note(8765, 50000, answer),
            "Another copy of Fel's Voice Lab (version 0.4.0) is using port 8765, so this Lab listens on port 50000 instead.",
        )
        self.assertEqual(
            lab_address.moved_note(8765, 50000, None),
            "Another program is using port 8765, so this Lab listens on port 50000 instead.",
        )

    def known_port(self, port):
        self.enterContext(mock.patch.object(lab_address, "KNOWN_PORT", port))
        self.enterContext(mock.patch.object(lab_address, "KNOWN_URL", f"http://127.0.0.1:{port}"))

    def test_the_service_has_its_port_to_itself(self):
        server, note = serve.bind_service(0, "id")
        self.addCleanup(server.server_close)
        self.assertIsNone(note)
        port = server.server_address[1]
        for reuse in (False, True):
            with socket.socket() as other:
                if reuse:
                    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    other.bind(("127.0.0.1", port))

    def test_another_program_on_the_known_port_moves_the_service_and_says_why(self):
        holder = socket.socket()
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        self.known_port(holder.getsockname()[1])
        server, note = serve.bind_service(lab_address.KNOWN_PORT, "id", ask=lambda url: None)
        self.addCleanup(server.server_close)
        self.assertNotEqual(server.server_address[1], lab_address.KNOWN_PORT)
        self.assertEqual(note, f"Another program is using port {lab_address.KNOWN_PORT}, so this Lab listens on port "
                               f"{server.server_address[1]} instead.")  # fmt: skip

    def test_this_lab_already_on_the_known_port_starts_nothing_more(self):
        first, _ = serve.bind_service(0, "id")
        self.addCleanup(first.server_close)
        self.known_port(first.server_address[1])
        answer = {"app": "Fel's Voice Lab", "version": "0.4.0", "status": "ready", "lab_id": "this-lab"}
        with self.assertRaisesRegex(serve.AlreadyRunning, "already running from this folder"):
            serve.bind_service(lab_address.KNOWN_PORT, "this-lab", ask=lambda url: answer)
        other, note = serve.bind_service(lab_address.KNOWN_PORT, "another-lab", ask=lambda url: answer)
        self.addCleanup(other.server_close)
        self.assertIn("Another copy of Fel's Voice Lab (version 0.4.0)", note)

    def test_any_other_port_must_be_free(self):
        first, _ = serve.bind_service(0, "id")
        self.addCleanup(first.server_close)
        with self.assertRaisesRegex(SystemExit, "cannot be used"):
            serve.bind_service(first.server_address[1], "id", ask=lambda url: self.fail("only the known port is asked"))


if __name__ == "__main__":
    unittest.main()
