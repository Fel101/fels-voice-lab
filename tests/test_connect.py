"""Connect other apps: what /api/health says about the Lab, the System page's routes for the panel, and the desktop window
opening on this Lab's own service when one already runs on its port."""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service  # noqa: E402
from app_version import VERSION  # noqa: E402
import desktop  # noqa: E402
import lab_address  # noqa: E402
import serve  # noqa: E402


class HealthTests(unittest.TestCase):
    def test_health_names_the_app_and_this_lab_folder_with_its_library(self):
        service = api_service.Service(self)
        health = service.get_json("/api/health")
        self.assertEqual(health["app"], "Fel's Voice Lab")
        self.assertEqual(health["lab_id"], lab_address.lab_id(serve.APP_ROOT, service.cast.root))
        self.assertTrue(lab_address.is_lab(health))


class ConnectRouteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(serve, "APP_ROOT", self.root))
        self.enterContext(mock.patch.dict(os.environ, {"VOICE_LAB_ADDRESS_FILE": str(self.root / "service.json")}))
        self.service = api_service.Service(self)

    def test_the_panel_hears_where_this_service_is_and_how_to_test_it(self):
        answer = self.service.get_json("/api/system/connect")
        self.assertEqual(
            answer,
            {
                "url": self.service.base,
                "known_url": "http://127.0.0.1:8765",
                "note": None,
                "in_address_file": False,
                "address_file": str(self.root / "service.json"),
                "window_port": 8765,
                "test": f"curl.exe {self.service.base}/api/health",
                "guide": f"https://github.com/Fel101/fels-voice-lab/blob/v{VERSION}/docs/API.md",
                "mcp": lab_address.mcp_settings(ROOT, sys.executable),
                "connect_guide": f"https://github.com/Fel101/fels-voice-lab/blob/v{VERSION}/docs/CONNECT.md",
                "openai_base": f"{self.service.base}/v1",
            },
        )
        line = answer["mcp"]["claude_code"]
        self.assertEqual(
            line,
            f'claude mcp add --scope user --transport stdio voice-lab -- "{sys.executable}" "{ROOT / "tools" / "mcp_server.py"}"',
        )
        self.assertEqual(
            json.loads(answer["mcp"]["settings"]),
            {"mcpServers": {"voice-lab": {"command": sys.executable, "args": [str(ROOT / "tools" / "mcp_server.py")]}}},
        )
        lab = self.service.server.RequestHandlerClass.lab
        lab.address_note, lab.in_address_file = "Another program is using port 8765, so ...", True
        answer = self.service.get_json("/api/system/connect")
        self.assertEqual((answer["note"], answer["in_address_file"]), ("Another program is using port 8765, so ...", True))

    def test_the_windows_port_is_kept_beside_the_lab_for_next_time(self):
        status, _headers, raw = self.service.request("POST", "/api/system/connect/port", {"port": 0})
        self.assertEqual((status, json.loads(raw)), (200, {"window_port": 0}))
        self.assertEqual(json.loads((self.root / "desktop.local.json").read_text("utf-8")), {"port": 0})
        self.assertEqual(self.service.get_json("/api/system/connect")["window_port"], 0)
        status, _headers, raw = self.service.request("POST", "/api/system/connect/port", {"port": 80})
        self.assertEqual(status, 400)
        self.assertIn("1024 to 65535", json.loads(raw)["error"])


class JoiningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def backend(self, port=8765):
        return desktop.DesktopBackend(root=self.root, port=port, sessions=self.root / "sessions")

    def test_the_window_opens_on_this_labs_own_service_and_no_other(self):
        backend = self.backend()
        mine = {"app": "Fel's Voice Lab", "status": "ready", "lab_id": lab_address.lab_id(self.root, self.root)}
        self.assertEqual(backend.find_running(ask=lambda url: mine), "http://127.0.0.1:8765")
        for answer in (None, {"status": "ok"}, {**mine, "lab_id": "another-lab-or-library"}):
            self.assertIsNone(backend.find_running(ask=lambda url: answer), answer)
        self.assertIsNone(self.backend(port=0).find_running(ask=lambda url: mine), "any free port: nothing to join")

    def test_a_joined_service_is_left_running_and_the_window_starts_none(self):
        backend = self.backend()
        backend.find_running = lambda: "http://127.0.0.1:8765"
        self.assertEqual(backend.start(), "http://127.0.0.1:8765")
        self.assertTrue(backend.joined)
        self.assertIsNone(backend.process)
        self.assertFalse((self.root / "sessions").exists(), "no session folder, no service of its own")
        backend.close()
        backend.close()

    def test_the_window_closes_when_the_joined_service_stops_and_its_watch_ends_with_the_window(self):
        backend = self.backend()
        backend.joined, backend.url = True, "http://127.0.0.1:8765"
        answers = iter([{"status": "ready"}, None, {"status": "ready"}, None, None, None])
        closed = []
        backend.wait_for_update(lambda: closed.append(True), ask=lambda url: next(answers), pause=0)
        self.assertEqual((closed, backend.joined_stopped), ([True], True), "three misses in a row, not two")
        watching = self.backend()
        watching.joined, watching.url = True, "http://127.0.0.1:8765"
        thread = threading.Thread(target=watching.wait_for_update, args=(self.fail,), kwargs={"ask": lambda url: {}})
        thread.start()
        watching.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(watching.joined_stopped)

    def test_the_service_is_asked_for_the_windows_port(self):
        backend = self.backend(port=9123)
        backend.python.parent.mkdir(parents=True)
        backend.python.write_bytes(b"")
        started = []

        class Stop(Exception):
            pass

        def popen(command, **options):
            started.append(command)
            raise Stop

        with (
            mock.patch.object(desktop.subprocess, "Popen", popen),
            mock.patch.object(desktop.DesktopBackend, "find_running", lambda self, ask=None: None),
            self.assertRaises(Stop),
        ):
            backend.start()
        command = started[0]
        self.assertEqual(command[command.index("--port") + 1], "9123")

    def test_the_window_reads_its_port_from_the_settings(self):
        source = (ROOT / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("port=lab_address.window_port(ROOT)", source)


if __name__ == "__main__":
    unittest.main()
