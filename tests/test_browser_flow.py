"""Browser flow: clone a voice, perform a line, build and render a scene, then export it. A second, shorter test opens the
page before Kokoro is installed and speaks after.

The served page runs in headless Microsoft Edge or Google Chrome, driven over the DevTools protocol with
the standard library only. Engines are fake (the same speak contract as the server tests), so no model
loads. The test is skipped when no Chromium-family browser is found. Set VOICE_LAB_BROWSER to a browser
path, and VOICE_LAB_BROWSER_SHOTS to a folder to keep a picture of the working part of the page at each stage
(the screenshots in the README are made this way).
"""

import base64
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ffmpeg_tools
import serve
from app_version import VERSION
from cast_store import CastStore
from evaluation_store import EvaluationStore
from scene_store import SceneBatchRunner, SceneStore
from worker_pool import read_catalog

BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
STATUS_IDS = ["err", "performError", "performStatus", "referenceStatus", "sceneError", "sceneStatus",
              "exportError", "exportStatus", "installState"]


def find_browser():
    candidates = [os.environ.get("VOICE_LAB_BROWSER"), shutil.which("msedge"), shutil.which("chrome"), *BROWSER_PATHS]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


CTRL, SHIFT, ALT = 2, 8, 1  # DevTools modifier bits
SAVED_LINE_IDS = ("fetch('/api/scenes/' + JSON.parse(document.getElementById('sceneEditor').value).scene_id)"
                  ".then((r) => r.json()).then((j) => j.scene.lines.map((l) => l.line_id).join(','))")
BROWSER = find_browser()
FFMPEG_READY = bool(ffmpeg_tools.find_ffmpeg() and ffmpeg_tools.find_ffprobe())  # what the page's export will find
SHOTS = Path(os.environ["VOICE_LAB_BROWSER_SHOTS"]) if os.environ.get("VOICE_LAB_BROWSER_SHOTS") else None


def wav_bytes(seconds=1.0, sample_rate=24_000, amplitude=0.1):
    time_axis = np.arange(round(seconds * sample_rate), dtype=np.float32) / sample_rate
    buffer = io.BytesIO()
    sf.write(buffer, amplitude * np.sin(2 * np.pi * 220 * time_axis), sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def scene_text_is(text):
    return f"JSON.parse(document.getElementById('sceneEditor').value).lines[0].text === {json.dumps(text)}"


def scene_line_ids_are(ids):
    return f"JSON.parse(document.getElementById('sceneEditor').value).lines.map((l) => l.line_id).join(',') === {json.dumps(ids)}"


class BrowserFakeEngines:
    """Fake worker with the speak, health and catalog contract that the server and page use."""

    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.requests = []

    def health(self):
        return {"device": self.device, "workers": [], "loaded": self.loaded}

    def engine_status(self):
        return {"device": self.device, "busy": False, "engines": [], "speed": []}

    def speak(self, request):
        self.requests.append(dict(request))
        entry = self.entries[request["engine"]]
        time_axis = np.arange(12_000, dtype=np.float32) / 24_000
        audio = (0.08 * np.sin(2 * np.pi * 260 * time_axis)).astype(np.float32)
        return audio, 24_000, {"total_s": 0.5, "mode": request.get("mode", "clone"), "backend": "browser-fake-worker",
                               "checkpoint": entry["checkpoint"], "checkpoint_revision": entry["local_revision"]}

    def close(self):
        pass


class WebSocket:
    """Minimal RFC 6455 client: masked text frames out, text messages in, pings answered."""

    def __init__(self, url, timeout=30):
        host_port, _, path = url[len("ws://"):].partition("/")
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                           ).encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("DevTools closed the websocket during the handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode("latin-1"))

    def _exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("DevTools closed the websocket")
            data += chunk
        return data

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        message = bytearray()
        while True:
            first, second = self._exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if second & 0x80 else None
            payload = self._exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError("DevTools closed the websocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return message.decode("utf-8")

    def close(self):
        self.sock.close()


class DevToolsPage:
    """One page of a headless Chromium-family browser, driven with JavaScript and real input events."""

    def __init__(self, browser):
        self.profile = tempfile.mkdtemp(prefix="voice-lab-devtools-")
        self.browser_url = None
        # Scrollbars hidden: a picture taller than the window is drawn without one, which would widen the page. Updates and
        # background requests are off: a new profile would otherwise download hundreds of megabytes of browser components.
        self.process = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--window-size=1280,900", "--hide-scrollbars", "--disable-component-update", "--disable-background-networking",
             "--remote-debugging-port=0", f"--user-data-dir={self.profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.problems = []
        self.failed, self.requests, self.port = [], {}, None  # the requests that failed while a page was opened (see open)
        self.next_id = 0
        try:
            self.socket = WebSocket(self._page_url())
            for domain in ("Runtime.enable", "Page.enable"):
                self.call(domain)
        except Exception:
            self.close()
            raise

    def _page_url(self):
        """Wait for the browser to open its DevTools port, remember its browser endpoint, and return the page's."""
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        pages = []
        while time.monotonic() < deadline:
            try:
                # Port first, then the browser endpoint. Windows refuses a read while the browser is still writing the file.
                port, browser_path = port_file.read_text(encoding="utf-8").splitlines()[:2]
                self.browser_url = f"ws://127.0.0.1:{port}{browser_path}"
                self.port = port
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10).read())
                pages = [target for target in targets if target["type"] == "page"]
                # The tab the browser was started with, not just the first page: Edge lists a page of its own (a sync dialog)
                # within a second of starting, and once it is there it comes first.
                return next(page["webSocketDebuggerUrl"] for page in pages if page["url"] == "about:blank")
            except (OSError, ValueError, StopIteration):
                time.sleep(0.1)
        raise RuntimeError(f"the browser did not open a DevTools port with a blank tab to drive (pages: {[p['url'] for p in pages]})")

    def call(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        self.socket.send_text(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv_text())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})
            self._remember(message)

    def _remember(self, message):
        params = message.get("params", {})
        method = message.get("method")
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            self.problems.append(details.get("exception", {}).get("description") or details.get("text", "page error"))
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            self.problems.append(" ".join(str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", [])))
        elif method == "Network.requestWillBeSent":
            self.requests[params.get("requestId")] = params.get("request", {}).get("url", "?")
        elif method == "Network.loadingFailed":
            self.failed.append(f"{self.requests.get(params.get('requestId'), '?')}: {params.get('errorText')}")
        elif method == "Network.responseReceived" and params.get("response", {}).get("status", 0) >= 400:
            self.failed.append(f"{params['response'].get('url')}: HTTP {params['response']['status']}")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise RuntimeError(details.get("exception", {}).get("description") or details.get("text"))
        return result["result"].get("value")

    def wait_until(self, expression, timeout=60):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                if self.evaluate(expression):
                    return
            except RuntimeError as error:
                last_error = error
            time.sleep(0.1)
        try:
            status = self.status()
        except RuntimeError as error:
            status = f"status unavailable: {error}"
        raise AssertionError(f"timed out waiting for {expression}\nlast error: {last_error}\n{status}")

    def status(self):
        return self.evaluate("(() => " + json.dumps(STATUS_IDS) + ".map((id) => {"
                             " const el = document.getElementById(id);"
                             " return el && el.textContent ? id + ': ' + el.textContent : ''; }"
                             ").filter(Boolean).join('\\n'))()")

    def look(self):
        """What the browser is showing, for a failure message: its tabs, how far this page got and what went wrong."""
        try:
            listing = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=5).read()
            tabs = [target["url"] for target in json.loads(listing) if target["type"] == "page"]
        except (OSError, ValueError) as error:
            tabs = f"unavailable ({error})"
        try:
            page = self.evaluate("[location.href, document.readyState, document.title, document.scripts.length].join(' | ')")
        except (RuntimeError, OSError) as error:
            page = f"no answer ({error})"
        return (f"browser tabs: {tabs}\nthis page (address | state | title | scripts): {page}\n"
                f"failed requests: {self.failed[-10:] or 'none'}\npage errors: {self.problems[-10:] or 'none'}")

    def open(self, url, patience=(30, 60)):
        """Load the page and wait until its scripts have run. The first load in a fresh browser on a slow computer can stall
        (a CI runner once left it for 60 s with nothing to show), so a page that is not ready within the first wait is
        loaded once more; one that never gets ready is reported with what the browser was showing. The errors of a load that
        did not get ready are kept in its note, not in `problems`: the page that came up is the one tested."""
        self.failed, self.requests = [], {}
        earlier = len(self.problems)  # an earlier page's errors stay
        self.call("Network.enable")  # only while the page loads: it is what says which request failed
        notes = []
        try:
            for attempt, seconds in enumerate(patience, start=1):
                started = self.call("Page.navigate", {"url": url}) if attempt == 1 else self.load_again(url)
                try:
                    return self.wait_until("document.documentElement.classList.contains('workspace-ready')", timeout=seconds)
                except AssertionError:
                    notes.append(f"load {attempt}, after {seconds} s: the browser answered {started}\n{self.look()}")
                    del self.problems[earlier:]
            raise AssertionError(f"{url} was not ready after {len(patience)} loads\n" + "\n".join(notes))
        finally:
            try:
                self.call("Network.disable")
            except (RuntimeError, OSError):
                pass

    def load_again(self, url):
        """Ask for a page that did not get ready once more. One that came up at its address but broken (a script it asked for
        was refused, say) is reloaded: navigating to the address it is already at is only a same-document navigation, answered
        without a loader id, so the broken page would stay as it is. One that never came up is navigated to again."""
        try:
            at = str(self.evaluate("location.href") or "")
        except (RuntimeError, OSError):
            at = ""
        if at.split("#")[0] == url.split("#")[0]:
            return self.call("Page.reload", {"ignoreCache": True})
        return self.call("Page.navigate", {"url": url})

    def show(self, section):
        self.evaluate(f"location.hash = {json.dumps('#' + section)}")
        self.wait_until(f"!document.getElementById({json.dumps(section)}).hidden")

    def set_value(self, selector, value):
        """A list's options can come from an answer the page is still waiting for, so a list waits for the option first."""
        self.wait_until(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            return !!el && (el.tagName !== 'SELECT' || [...el.options].some((o) => o.value === {json.dumps(value)}));
        }})()""")
        self.evaluate(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('no element {selector}');
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})()""")

    def choose(self, selector, text):
        self.wait_until(f"""(() => {{
            const select = document.querySelector({json.dumps(selector)});
            return !!select && [...select.options].some((o) => o.textContent === {json.dumps(text)});
        }})()""")
        self.evaluate(f"""(() => {{
            const select = document.querySelector({json.dumps(selector)});
            const wanted = {json.dumps(text)};
            const option = [...select.options].find((item) => item.textContent === wanted);
            if (!option) throw new Error('no option ' + wanted + ' in {selector}');
            select.value = option.value;
            select.dispatchEvent(new Event('change', {{bubbles: true}}));
            return option.value;
        }})()""")

    def set_checked(self, selector, checked):
        self.evaluate(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            el.checked = {"true" if checked else "false"};
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})()""")

    def click(self, selector):
        self.evaluate(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('no element {selector}');
            el.click();
            return true;
        }})()""")

    def set_files(self, selector, path):
        """Give a file input one file, built in the page, then fire its change event like a file picker would."""
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        self.evaluate(f"""(() => {{
            const input = document.querySelector({json.dumps(selector)});
            if (!input) throw new Error('no file input {selector}');
            const bytes = Uint8Array.from(atob({json.dumps(data)}), (c) => c.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], {json.dumps(Path(path).name)}, {{type: 'audio/wav'}}));
            input.files = transfer.files;
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            return input.files.length;
        }})()""")

    def press(self, key, code, key_code, modifiers=0):
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {"type": kind, "key": key, "code": code,
                                                 "windowsVirtualKeyCode": key_code,
                                                 "nativeVirtualKeyCode": key_code, "modifiers": modifiers})

    def screenshot(self, name, selector, to=None):
        """Keep a PNG of the page from `selector` down to `to` (just `selector` when `to` is left out), if
        VOICE_LAB_BROWSER_SHOTS names a folder. It is drawn at twice the size, so it stays sharp on a dense screen."""
        if SHOTS is None:
            return
        SHOTS.mkdir(parents=True, exist_ok=True)
        box = self.evaluate(f"""(() => {{
            const first = document.querySelector({json.dumps(selector)});
            const last = document.querySelector({json.dumps(to or selector)});
            if (!first || !last) throw new Error('no element for the screenshot {name}');
            const a = first.getBoundingClientRect(), b = last.getBoundingClientRect();
            const x = Math.max(0, Math.min(a.left, b.left) + scrollX - 4), y = Math.max(0, a.top + scrollY - 4);
            return {{x, y, width: Math.max(a.right, b.right) + scrollX + 4 - x, height: b.bottom + scrollY + 4 - y}};
        }})()""")
        data = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True,
                                                    "clip": {**box, "scale": 2}})["data"]
        (SHOTS / f"{name}.png").write_bytes(base64.b64decode(data))

    def close(self):
        if hasattr(self, "socket"):
            self.socket.close()
        if self.browser_url:
            # Edge on Windows hands its real browser to a process of its own, so ending the one started here would leave
            # the browser running (and its profile in use). Ask the browser to quit.
            try:
                browser = WebSocket(self.browser_url, timeout=5)
                browser.send_text(json.dumps({"id": 1, "method": "Browser.close"}))
                browser.recv_text()
                browser.close()
            except OSError:
                pass
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            self.process.kill()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(50):  # a browser that is still quitting keeps its profile open for a moment
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                break
            time.sleep(0.2)


class FlowServer:
    """The real request handler over temporary stores, with the fake worker and a running batch runner.

    With `hold`, a threading.Event, every GET /api/ answer waits until it is set, as on a slow computer: the page and its
    scripts arrive, but none of the data they ask for. close() sets it, so no answer is left waiting."""

    def __init__(self, root, hold=None):
        self.cast = CastStore(root)
        self.engines = BrowserFakeEngines()
        self.server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.scenes = SceneStore(self.cast, self.engines.entries, root=root)
        self.runner = SceneBatchRunner(self.scenes, self.base)
        evaluations = EvaluationStore(root / "data" / "evaluations" / "evaluations.sqlite3", lab_root=root)
        handler = serve.make_handler(self.engines, port, self.cast, self.scenes, self.runner, evaluations)
        self.hold = hold
        if hold is not None:

            class Held(handler):
                def do_GET(self):
                    if self.path.startswith("/api/"):
                        hold.wait(60)
                    super().do_GET()

            handler = Held
        self.server.RequestHandlerClass = handler
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.runner.start()

    def close(self):
        if self.hold is not None:
            self.hold.set()
        self.runner.close(timeout=5)
        self.server.shutdown()
        self.server.server_close()


class ScenePreviewRouteTests(unittest.TestCase):
    """Preview audio is served from the scene store's own folder, which need not be the project's out/ folder."""

    def test_preview_audio_is_served_from_the_state_root(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-preview-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            try:
                scene_id = "b" * 32
                folder = server.scenes.output_root / scene_id
                folder.mkdir(parents=True)
                (folder / "scene-r1-abcd1234.wav").write_bytes(b"RIFF-preview")
                with urllib.request.urlopen(f"{server.base}/api/scenes/{scene_id}/previews/scene-r1-abcd1234.wav",
                                            timeout=10) as response:
                    self.assertEqual(response.read(), b"RIFF-preview")
                    self.assertEqual(response.headers["Content-Type"], "audio/wav")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{server.base}/api/scenes/{scene_id}/previews/missing.wav", timeout=10)
                self.assertEqual(caught.exception.code, 404)
            finally:
                server.close()


class OpenRetryTests(unittest.TestCase):
    """DevToolsPage.open asks once more for a page that did not get ready. The page here is a stand-in; no browser starts."""

    URL = "http://127.0.0.1:50000/#cast"

    def stand_in(self, address):
        """A page that says it is at `address`, is not ready the first time it is waited on, and notes what it is asked."""
        page = DevToolsPage.__new__(DevToolsPage)
        page.failed, page.requests, page.asked, page.problems = [], {}, [], []
        waits = [AssertionError("not ready"), None]

        def wait_until(expression, timeout=60):
            outcome = waits.pop(0)
            if outcome:
                raise outcome

        page.call = lambda method, params=None: page.asked.append(method) or {}
        page.evaluate = lambda expression: address
        page.wait_until, page.look = wait_until, lambda: "(what the browser showed)"
        return page

    def test_a_page_that_came_up_broken_is_reloaded_not_navigated_to_again(self):
        page = self.stand_in(self.URL)
        page.open(self.URL, patience=(1, 1))
        self.assertEqual(page.asked, ["Network.enable", "Page.navigate", "Page.reload", "Network.disable"])

    def test_a_page_that_never_came_up_is_navigated_to_again(self):
        page = self.stand_in("about:blank")
        page.open(self.URL, patience=(1, 1))
        self.assertEqual(page.asked, ["Network.enable", "Page.navigate", "Page.navigate", "Network.disable"])

    def test_the_errors_of_a_load_that_did_not_get_ready_are_left_with_it(self):
        """A CI runner refused one script on the first load, so the next script stopped on "generate is not defined" and the
        page was loaded again. That error belongs to the broken load; the page that came up is the one tested. An earlier
        page's errors stay."""
        page = self.stand_in(self.URL)
        page.problems.append("an earlier page's error")
        not_ready_once = page.wait_until

        def wait_until(expression, timeout=60):
            if page.asked == ["Network.enable", "Page.navigate"]:
                page.problems.append("ReferenceError: generate is not defined")
            not_ready_once(expression, timeout)

        page.wait_until = wait_until
        page.open(self.URL, patience=(1, 1))
        self.assertEqual(page.problems, ["an earlier page's error"])


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class BrowserFlowTests(unittest.TestCase):
    def test_clone_perform_edit_scene_and_export_in_the_page(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            reference = root / "mira-reference.wav"
            reference.write_bytes(wav_bytes())
            server = FlowServer(root)
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#cast")
                self.assertEqual(page.evaluate("document.getElementById('appVersion').textContent"), VERSION)

                # Clone: a character, a candidate voice, one reference recording, then approval.
                page.set_value("#characterName", "Mira")
                page.click("#createCharacter")
                page.wait_until("[...document.querySelectorAll('#characterSelect option')].some((o) => o.textContent === 'Mira')")
                page.choose("#characterSelect", "Mira")
                page.set_value("#identityEngine", "qwen3-tts-1.7b-base")
                page.set_value("#identityLabel", "Mira base")
                page.click("#createIdentity")
                page.wait_until("document.getElementById('identityState').textContent.startsWith('qwen3-tts-1.7b-base')")
                page.set_files("#referenceFile", reference)
                page.wait_until("document.getElementById('referenceStatus').textContent.startsWith('Preview ready')")
                page.set_value("#referenceTranscript", "A clear reference line.")
                page.set_value("#referenceOrigin", "Browser flow fixture")
                page.set_value("#referenceAllowedUse", "Commercial use permitted with credit.")
                page.wait_until("!document.getElementById('saveReference').disabled")
                page.click("#saveReference")
                # The save message is replaced when the cast refreshes, so wait for the version in the approval list.
                page.wait_until("document.getElementById('approveReference').options.length > 0")
                page.click("#approveIdentity")
                page.wait_until("document.getElementById('identityState').textContent.includes('approved recording v1')")

                # Perform: a set of two takes of one line from the approved voice, made one after the other.
                page.show("perform")
                page.choose("#performCharacter", "Mira")
                page.wait_until("document.getElementById('performIdentity').value !== ''")
                self.assertTrue(page.evaluate("!document.getElementById('variantCount').parentElement.hidden"))
                page.set_value("#variantCount", "2")
                page.click("#performGenerate")
                page.wait_until("document.querySelectorAll('#takeList audio').length >= 2")
                page.wait_until("document.getElementById('performStatus').textContent.startsWith('Takes made.')")
                self.assertTrue(page.evaluate("document.getElementById('performStop').hidden"
                                              " && !document.getElementById('performGenerate').disabled"))
                page.screenshot("perform", "#perform", to="#takeList article")

                # Edit: open the editor on a take, hear a trimmed, faded and quieter copy, and save it as a new take.
                originals = {take["id"]: take["audio_sha256"] for take in server.cast.list_takes()}
                self.assertEqual(len(originals), 2)
                edit_field = "#takeList .take-edit [data-edit-field={}]".format

                def edit_button(text):
                    return ("[...document.querySelectorAll('#takeList .take-edit button')]"
                            f".find((b) => b.textContent === {json.dumps(text)} && !b.disabled)")

                page.evaluate("[...document.querySelectorAll('#takeList article button')]"
                              ".find((b) => b.textContent === 'Edit take…').click()")
                page.wait_until("!!" + edit_button("Hear the edit"))
                page.wait_until("document.querySelector('#takeList .take-edit-speech').textContent.startsWith('Speech from')")
                for name, value in (("start_seconds", "0.05"), ("end_seconds", "0.4"), ("fade_in_ms", "20"), ("fade_out_ms", "50"),
                                    ("gain_db", "-3")):
                    page.set_value(edit_field(name), value)
                page.evaluate(edit_button("Hear the edit") + ".click()")
                page.wait_until("document.querySelector('#takeList .take-edit [role=status]').textContent.startsWith('The edited clip is 0.35 s')")
                self.assertTrue(page.evaluate("document.querySelector('#takeList .take-edit audio').src.startsWith('blob:')"))
                page.screenshot("take-editor", "#takeList article:has(.take-edit)")
                page.evaluate(edit_button("Save as new take") + ".click()")
                page.wait_until("document.getElementById('performStatus').textContent.startsWith('Saved as a new take, 0.35 s')")
                page.wait_until("document.querySelectorAll('#takeList article').length === 3")
                self.assertTrue(page.evaluate("[...document.querySelectorAll('#takeList article')]"
                                              ".some((a) => a.textContent.includes('Edited from take'))"))
                saved = server.cast.list_takes()
                self.assertEqual(len(saved), 3)
                edited = next(take for take in saved if take["id"] not in originals)
                self.assertIn(edited["parent_take_id"], originals)
                self.assertAlmostEqual(edited["duration_seconds"], 0.35, places=3)
                self.assertEqual({take["id"]: take["audio_sha256"] for take in saved if take["id"] in originals}, originals)

                # Cast again: the voice card now plays one of its takes and names the model that made it.
                page.show("cast")
                page.click("#refreshVoices")
                page.wait_until("document.querySelector('#voiceGallery article audio') !== null")
                self.assertIn("Created with Qwen Base 1.7B", page.evaluate("document.getElementById('voiceGallery').textContent"))
                self.assertIn("Not cleared", page.evaluate("document.querySelector('#voiceGallery .licence-badge').textContent"))
                page.evaluate("[...document.querySelectorAll('#voiceGallery button')].find((b) => b.textContent === 'Edit voice').click()")
                page.wait_until("document.getElementById('castEditor').open && !document.getElementById('referenceLicence').hidden")
                page.set_value("#referenceLicenceRating", "conditions")
                page.evaluate("document.getElementById('referenceLicenceRating').focus()")
                page.press("Tab", "Tab", 9)
                self.assertEqual(page.evaluate("document.activeElement.id"), "saveReferenceLicence")
                page.press(" ", "Space", 32)
                page.wait_until("document.querySelector('#voiceGallery .licence-badge.conditions') !== null")
                self.assertIn("Allowed, with conditions", page.evaluate("document.querySelector('#voiceGallery .licence-badge').textContent"))
                page.show("perform")
                page.wait_until("document.querySelectorAll('#takeList .licence-badge.conditions').length === 3")
                page.show("cast")
                page.screenshot("cast-library", "#cast", to="#voiceGallery")

                # Scene: start from the performer, save, edit, render from the table, choose the takes, re-render a ticked
                # line, and assemble.
                page.show("scenes")
                page.click("#sceneStarter")
                page.click("#sceneSave")
                # The picker fills one request before the saved scene is drawn, and that draw clears the undo history and the editor.
                # The status line names the revision only after it, so wait for that before editing.
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 1 && document.getElementById('sceneSelect').value"
                                " && document.getElementById('sceneStatus').textContent.includes('/ revision 1 /')")
                # Scene editing: one change is one undo step; Ctrl+Z, Ctrl+Shift+Z, Ctrl+Y and the buttons work;
                # Alt+Down moves the focused line; Ctrl+S saves; the shortcuts stay off outside the Scenes workspace.
                dialogue = '#sceneLineEditor textarea[data-scene-field="text"]'
                page.wait_until(f"document.querySelector({json.dumps(dialogue)}) !== null")
                original = page.evaluate(f"document.querySelector({json.dumps(dialogue)}).value")
                page.set_value(dialogue, "Changed line.")
                page.wait_until("document.getElementById('sceneEditor').value.includes('Changed line.')")
                page.press("z", "KeyZ", 90, CTRL)
                page.wait_until(scene_text_is(original))
                page.press("Z", "KeyZ", 90, CTRL | SHIFT)
                page.wait_until(scene_text_is("Changed line."))
                page.press("z", "KeyZ", 90, CTRL)
                page.wait_until(scene_text_is(original))
                page.press("y", "KeyY", 89, CTRL)
                page.wait_until(scene_text_is("Changed line."))
                page.click("#sceneUndo")
                page.wait_until(scene_text_is(original))
                page.wait_until("document.getElementById('sceneUndo').disabled && !document.getElementById('sceneRedo').disabled")
                page.click("#sceneRedo")
                page.wait_until(scene_text_is("Changed line."))

                page.click("#sceneLineAdd")
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 2")
                page.evaluate(f"document.querySelector({json.dumps(dialogue)}).focus()")
                page.press("ArrowDown", "ArrowDown", 40, ALT)
                page.wait_until(scene_line_ids_are("line-002,line-001"))
                page.wait_until("document.activeElement && document.activeElement.classList.contains('scene-open')")
                page.press("s", "KeyS", 83, CTRL)
                page.wait_until("document.getElementById('sceneStatus').textContent.includes('revision')")
                self.assertEqual(page.evaluate(SAVED_LINE_IDS), "line-002,line-001")

                page.click('button[aria-label="Remove line line-002"]')
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 1")
                before_outside = page.evaluate("document.getElementById('sceneEditor').value")
                page.show("perform")
                page.press("z", "KeyZ", 90, CTRL)
                self.assertEqual(page.evaluate("document.getElementById('sceneEditor').value"), before_outside)
                page.show("scenes")
                page.press("z", "KeyZ", 90, CTRL)
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 2")
                page.click('button[aria-label="Remove line line-002"]')
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 1")

                # Put the first sentence back, so the render and the export are of the line the performer had.
                page.set_value(dialogue, original)
                page.wait_until(scene_text_is(original))

                # The table: a second line, Render all, the newest take chosen for both, then only the ticked second line
                # rendered again. It gets a second take; the first line keeps its one.
                page.click("#sceneLineAdd")
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 2")
                page.click("#sceneStart")
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line[data-status=\"choose_take\"]').length === 2")
                page.click("#sceneChooseNewest")
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line[data-status=\"done\"]').length === 2")
                self.assertTrue(page.evaluate("JSON.parse(document.getElementById('sceneEditor').value).lines"
                                              ".every((l) => l.selected_take_id)"))
                prefix = page.evaluate("JSON.parse(document.getElementById('sceneEditor').value).scene_id")[:12]
                page.click('input[aria-label="Tick line 2"]')
                page.wait_until("document.getElementById('sceneRenderTicked').textContent === 'Render ticked (1)'")
                page.click("#sceneRenderTicked")
                page.wait_until("document.getElementById('sceneStatus').textContent.startsWith('completed / 1 completed')")
                page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line[data-status=\"done\"]').length === 2")
                self.assertEqual({line: len(server.cast.list_takes(line_id=f"{prefix}:{line}")) for line in ("line-001", "line-002")},
                                 {"line-001": 1, "line-002": 2})
                self.assertEqual(page.evaluate("document.querySelectorAll('#sceneRegenerateIds').length"), 0,
                                 "no line id is typed anywhere")
                page.click("#scenePreview")
                page.wait_until("document.getElementById('scenePreviewPlayer').getAttribute('src')")
                self.assertTrue(page.evaluate("document.getElementById('sceneJobSelect').selectedOptions[0].textContent"
                                              ".startsWith('completed / 1/1 / ')"), "the job list follows the batch it started")
                page.click('button[aria-label="Line 1 details"]')
                page.screenshot("scene", "label:has(#sceneName)", to="#scenePreviewPlayer")

                # Export: write the package, then verify it. (Without FFmpeg the page says so, and that is checked instead.)
                page.evaluate("document.getElementById('sceneExport').open = true")
                if FFMPEG_READY:
                    page.wait_until("document.getElementById('exportCreate') && !document.getElementById('exportCreate').disabled")
                    page.click("#exportCreate")
                    page.wait_until("document.getElementById('exportStatus').textContent.startsWith('Package written to')", timeout=120)
                    # The status says "written" before the package list is refreshed, so wait for the list as a person would.
                    page.wait_until("document.getElementById('exportSelect').value !== ''")
                    page.click("#exportVerify")
                    page.wait_until("document.getElementById('exportStatus').textContent.startsWith('Verification passed')", timeout=120)
                    page.screenshot("export", "#sceneExport")

                    packages = sorted((root / "out" / "exports").iterdir())
                    self.assertEqual(len(packages), 1)
                    self.assertTrue((packages[0] / "manifest.json").is_file())
                else:
                    # A computer without FFmpeg (a CI runner, say) is told so in the page, which switches the export buttons off,
                    # so there is nothing to press and nothing is written. (A button pressed before the page knew would get the
                    # server's refusal instead, so pressing one here would be a race.)
                    page.wait_until("document.getElementById('exportFfmpeg').textContent.startsWith('FFmpeg was not found')")
                    self.assertTrue(page.evaluate("['exportPreview', 'exportCreate', 'exportVerify']"
                                                  ".every((id) => document.getElementById(id).disabled)"))
                    self.assertEqual(page.evaluate("document.getElementById('exportStatus').textContent"), "")
                    self.assertEqual(list((root / "out").glob("exports/*")), [])

                # System: the report, then a backup that is read back, checked by hand, and restored into a new folder.
                # (Cleanup and "open the restored library" are left alone: they act on the project's own folders.)
                page.show("system")
                page.wait_until("document.querySelectorAll('#systemFacts dt').length >= 6 && !document.getElementById('systemCopy').disabled")
                self.assertIn(VERSION, page.evaluate("document.getElementById('systemFacts').textContent"))
                self.assertIn(VERSION, page.evaluate("document.getElementById('systemDiagnostics').textContent"))
                page.wait_until("document.getElementById('systemFfmpeg').textContent !== ''")
                page.click("#backupCreate")
                page.wait_until("document.getElementById('backupStatus').textContent.startsWith('Backup saved as')", timeout=120)
                page.wait_until("document.querySelectorAll('#backupList article').length === 1")
                self.assertEqual(len(list((root / "backups").glob("voice-lab-library-*.zip"))), 1)
                page.evaluate("[...document.querySelectorAll('#backupList button')].find((b) => b.textContent === 'Check').click()")
                page.wait_until("document.getElementById('backupStatus').textContent.includes(' passed: every file matches')", timeout=120)
                page.evaluate("[...document.querySelectorAll('#backupList button')].find((b) => b.textContent === 'Restore…').click()")
                page.wait_until("!document.getElementById('restoreBox').hidden")
                page.set_value("#restoreFolder", "browser copy")
                page.click("#restoreGo")
                page.wait_until("document.getElementById('backupStatus').textContent.startsWith('Restored to')", timeout=120)
                self.assertTrue((root / "restored" / "browser copy" / "data" / "cast" / "cast.sqlite3").is_file())
                self.assertTrue(page.evaluate("!document.getElementById('restoreUse').hidden && !document.getElementById('restoreHint').hidden"))
                # (No picture of this page: it names the temporary folder and describes this computer.)
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class EmptyLibraryPageTests(unittest.TestCase):
    def test_home_and_empty_workspaces_point_to_the_next_step(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            server.engines.engine_status = lambda: {
                "device": "cpu", "busy": False, "speed": [], "engines": [
                    {"id": "kokoro", "name": "Kokoro 82M", "state": "not_installed",
                     "installation": {"installed": False, "installer": "setup-kokoro.bat"}},
                ],
            }
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/")
                page.wait_until("!$('home').hidden && !$('homeStart').hidden && !$('firstRun').hidden")
                self.assertEqual(page.evaluate("$('firstRun').parentElement.id"), "homeFirstRun")
                self.assertEqual(page.evaluate("$('homeRecent').children.length"), 0)
                self.assertTrue(page.evaluate("$('homePerform').hidden"))
                page.screenshot("empty-home", "#home")
                page.click("#homeImportScript")
                page.wait_until("!$('scenes').hidden && $('sceneScriptImport').open")
                self.assertTrue(page.evaluate("$('sceneSaved').hidden && $('sceneWork').hidden"))
                self.assertEqual(page.evaluate("document.querySelector('#sceneScriptImport summary').textContent"), "Import a script")
                page.screenshot("empty-scenes", "#scenes")
                page.click("#sceneStarter")
                page.wait_until("!$('perform').hidden && !$('performEmpty').hidden && $('performEditor').hidden")
                page.click("#performEmpty a")
                page.wait_until("!$('cast').hidden && !$('castEmpty').hidden && $('castBrowsing').hidden")
                page.click("#castCreateFirst")
                self.assertTrue(page.evaluate("$('castEditor').open && document.activeElement.id === 'characterName'"))
                page.open(server.base + "/compare")
                page.wait_until("!$('compareEmpty').hidden && $('compareSavedTakes').hidden && $('compareRuns').hidden")
                page.click("#compareEmpty a")
                page.wait_until("location.hash === '#perform' && !$('performEmpty').hidden")
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class GenerateAfterInstallTests(unittest.TestCase):
    def test_a_page_opened_before_kokoro_was_installed_speaks_once_it_is(self):
        installed = []  # the Kokoro voices on disk: none while the page opens, two once the installer has run
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp, \
                mock.patch.object(serve, "discover_kokoro_voices", lambda: list(installed)):
            server = FlowServer(Path(temp))
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#studio")
                page.wait_until("document.getElementById('engine').value === 'kokoro'")
                self.assertEqual(page.evaluate("document.getElementById('voice').options.length"), 0)
                installed.extend(["af_heart", "bf_emma"])  # the installer finishes while the page stays open
                page.click("#go")
                page.wait_until("document.querySelectorAll('#hist article').length === 1")
                self.assertEqual(page.evaluate("document.getElementById('err').textContent"), "")
                self.assertEqual(page.evaluate("[...document.getElementById('voice').options].map((o) => o.value).join(',')"),
                                 "af_heart,bf_emma")
                self.assertEqual(page.evaluate("[...document.getElementById('voice').selectedOptions].map((o) => o.value).join(',')"),
                                 "af_heart")
                self.assertEqual(page.evaluate("[...document.getElementById('fixedVoice').options].map((o) => o.value).join(',')"),
                                 "af_heart,bf_emma")
                self.assertEqual([request["voice"] for request in server.engines.requests], ["af_heart"])
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class InstallWindowTests(unittest.TestCase):
    def test_stop_with_the_keyboard_and_retry_from_the_model_card(self):
        from engine_install import EngineInstaller
        from tests.fixtures.interruptible_model_install import write_launcher

        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            write_launcher(root / "installer.py", "kokoro")
            (root / "hold").write_text("pip", "ascii")
            installer = None

            def setup(pool):
                nonlocal installer
                pool.busy = False

                def hold(*_, **_kwargs):
                    pool.busy = True

                def release():
                    pool.busy = False

                def probe(_):
                    return {"installed": (root / "check.pid").is_file(), "installer": "setup-kokoro.bat"}

                def status():
                    installation = probe(None)
                    return {"device": "cpu", "busy": pool.busy, "speed": [], "engines": [{
                        "id": "kokoro", "name": "Kokoro 82M", "installation": installation,
                        "state": "not_checked" if installation["installed"] else "not_installed",
                    }]}

                pool.hold, pool.release_hold, pool.install_probe, pool.engine_status = hold, release, probe, status
                installer = EngineInstaller(pool, root=root, python=sys.executable, scripts={"kokoro": "installer.py"})
                return installer

            with mock.patch.object(serve, "EngineInstaller", side_effect=setup):
                server = FlowServer(root)
            page = None
            card_button = "modelCards.kokoro.button"
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#models")
                page.wait_until(f"{card_button}.textContent === 'Install' && !{card_button}.disabled")
                page.evaluate("modelCards.kokoro.card.id = 'kokoro-test-card'")
                card = "#kokoro-test-card .model-actions button:first-child"
                page.click(card)
                page.wait_until("!$('installGo').hidden")
                self.assertTrue(page.evaluate("$('installStop').hidden"))
                page.click("#installGo")
                page.wait_until("installDialog.job?.phase === 'packages' && !$('installStop').hidden")
                page.evaluate("$('installClose').focus()")
                page.press("Tab", "Tab", 9, modifiers=SHIFT)
                self.assertEqual(page.evaluate("document.activeElement.id"), "installStop")
                page.press(" ", "Space", 32)
                page.wait_until("installDialog.job?.state === 'stopping' && $('installState').textContent.startsWith('Stopping after this package')")
                self.assertEqual(page.evaluate("document.activeElement.id"), "installClose")
                page.screenshot("install-stopping-after-package", "#installDialog")
                page.click("#installClose")
                page.wait_until(f"{card_button}.textContent === 'Show progress'")
                page.click(card)
                page.wait_until("installDialog.job?.state === 'stopping'")
                (root / "continue").touch()
                page.wait_until("installDialog.job?.state === 'stopped' && $('installState').textContent.includes('Choose Install')")
                page.click("#installClose")
                page.wait_until(f"{card_button}.textContent === 'Install' && !{card_button}.disabled")

                (root / "continue").unlink()
                (root / "hold").write_text("check", "ascii")
                self.assertTrue(page.evaluate("fetch('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json'},"
                                              "body:JSON.stringify({language:'es'})}).then(r=>r.ok)"))
                page.call("Page.reload", {"ignoreCache": True})
                page.wait_until(f"document.documentElement.lang === 'es' && {card_button}.textContent === 'Instalar'")
                page.evaluate("modelCards.kokoro.card.id = 'kokoro-test-card'")
                page.click(card)
                page.wait_until("!$('installGo').hidden")
                page.click("#installGo")
                page.wait_until("installDialog.job?.phase === 'checking' && !$('installStop').hidden")
                self.assertEqual(page.evaluate("$('installStop').textContent"), "Detener")
                page.wait_until("fetch('/api/engines/status').then(r=>r.json()).then(s=>s.engines[0].installation.installed)")
                page.click("#installStop")
                page.wait_until("installDialog.job?.state === 'stopped' && $('installState').textContent.includes('comprobación del modelo')")
                page.click("#installClose")
                page.wait_until(f"{card_button}.textContent === 'Instalar' && !{card_button}.disabled")
                page.screenshot("install-stopped-model-check", "#kokoro-test-card")

                (root / "continue").touch()
                page.click(card)
                page.wait_until("!$('installGo').hidden")
                page.click("#installGo")
                page.wait_until("installDialog.job?.state === 'finished'")
                page.click("#installClose")
                page.wait_until(f"{card_button}.textContent === 'Prueba'")
                self.assertTrue((root / "receipt").is_file())
                self.assertEqual(page.problems, [])
            finally:
                (root / "continue").touch()
                if page is not None:
                    page.close()
                server.close()
                if installer is not None:
                    installer.close()

    def test_the_install_window_shows_while_model_studio_is_open(self):
        """The first-run note in Model studio opens the same install window as a model's card, while Models & features is
        hidden. A modal dialog inside a hidden section opens with no size, and the page behind it stops answering."""
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#studio")
                page.wait_until("document.getElementById('models').hidden && !document.getElementById('studio').hidden")
                page.evaluate("openInstallDialog('kokoro', false, 'plan', 'Install Kokoro 82M') && true")
                size = page.evaluate("(() => { const box = document.getElementById('installDialog').getBoundingClientRect();"
                                     " return [box.width, box.height]; })()")
                self.assertTrue(page.evaluate("document.getElementById('installDialog').matches(':modal')"))
                self.assertGreater(size[0], 0)
                self.assertGreater(size[1], 0)
                page.click("#installClose")
                page.wait_until("!document.getElementById('installDialog').open")
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()

    def test_the_install_window_closes_before_the_page_has_heard_about_the_models(self):
        """The install window's buttons work as soon as the page is there. On a slow computer the first answer about the
        models can come seconds after the page, and Close used to do nothing until it had (a CI run failed on that)."""
        hold = threading.Event()
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp), hold=hold)
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#studio")
                page.evaluate("openInstallDialog('kokoro', false, 'plan', 'Install Kokoro 82M') && true")
                page.click("#installClose")
                page.wait_until("!document.getElementById('installDialog').open", timeout=5)
                self.assertEqual(page.evaluate("Object.keys(modelCards).length"), 0, "an answer about the models had arrived")
                hold.set()
                page.wait_until("Object.keys(modelCards).length > 0")
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class CastEditorTests(unittest.TestCase):
    def test_a_kokoro_voice_keeps_the_preset_voices_chosen_for_it(self):
        """The Cast editor sent the chosen voices where the service did not look, so every Kokoro voice made in the page was
        saved as af_heart, and no blend could be made there."""
        import kokoro_support

        installed = ["af_heart", "bf_emma", "bm_george"]
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp, \
                mock.patch.object(serve, "discover_kokoro_voices", lambda: list(installed)), \
                mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: list(installed)):
            server = FlowServer(Path(temp))
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#cast")
                page.evaluate("document.getElementById('castEditor').open = true")
                page.set_value("#characterName", "Tobin")
                page.click("#createCharacter")
                page.wait_until("[...document.querySelectorAll('#characterSelect option')].some((o) => o.textContent === 'Tobin')")
                page.set_value("#identityEngine", "kokoro")
                page.wait_until("document.getElementById('fixedVoice').options.length === 3")
                page.set_value("#identityLabel", "Tobin, a blend")
                page.evaluate("""[...document.getElementById('fixedVoice').options]
                                 .forEach((o) => { o.selected = ['bf_emma', 'bm_george'].includes(o.value); })""")
                page.click("#createIdentity")
                page.wait_until("document.getElementById('identityState').textContent.startsWith('kokoro')")
                voices = [identity["engine_options"]["voice"] for character in server.cast.list_cast()
                          for identity in character["identities"]]
                self.assertEqual(voices, ["bf_emma,bm_george"])
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class ScriptImportPageTests(unittest.TestCase):
    """A Fountain file and a pasted plain-text script each become a scene, with the speakers matched to Cast by name."""

    def test_a_fountain_file_and_a_pasted_script_become_scenes_in_the_page(self):
        from tests.test_scenes import tone_wav

        scripts = ROOT / "tests" / "fixtures" / "scripts"
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            for name, engine in (("Mara", "qwen3-tts-1.7b-base"), ("Tomas", "turbo")):
                character = server.cast.create_character(name, "")
                identity = server.cast.create_identity(character["id"], engine, f"{name} voice")
                ref = server.cast.add_reference(identity["id"], f"{name.lower()}.wav", tone_wav(6.0),
                                                transcript="A clear reference line.", origin="test", allowed_use="tests",
                                                edits={"target_sample_rate": 24_000})
                server.cast.update_identity(identity["id"], {"status": "approved", "reference_version_id": ref["id"]})
            page = None
            speakers = ("[...document.querySelectorAll('#sceneScriptSpeakers tr')].map((row) => row.cells[0].textContent + '='"
                        " + row.querySelector('select').selectedOptions[0].textContent).join('|')")
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#scenes")
                page.wait_until("document.querySelector('#sceneScriptFormat option[value=fountain]') && castCharacters.length === 2")
                page.set_files("#sceneImportFile", scripts / "harbour.fountain")
                page.wait_until("!document.getElementById('sceneScriptMatch').hidden")
                self.assertEqual(page.evaluate("document.getElementById('sceneScriptSummary').textContent"),
                                 "14 lines, 3 speakers, 4 lines of action, 0 lines not understood.")
                self.assertEqual(page.evaluate(speakers), "MARA=Mara voice · Qwen3-TTS 1.7B Base|TOMAS=Tomas voice · Chatterbox "
                                 "Turbo 350M|McCLANE=Choose a voice…|Action, read by a narrator=Leave these lines out")
                # A speaker with no voice, and not left out, keeps the button off.
                self.assertTrue(page.evaluate("document.getElementById('sceneScriptCreate').disabled"))
                self.assertEqual(page.evaluate("document.getElementById('sceneScriptWaiting').textContent"),
                                 "Choose a voice for McCLANE, or leave their lines out.")
                page.set_value("#sceneScriptSpeakers tr:nth-child(3) select", "-")
                page.wait_until("!document.getElementById('sceneScriptCreate').disabled")
                page.click("#sceneScriptCreate")
                page.wait_until("document.getElementById('sceneName').value === 'The Harbour Light'"
                                " && document.querySelectorAll('.scene-table tbody tr.scene-row').length === 9")
                self.assertTrue(page.evaluate("document.getElementById('sceneScriptMatch').hidden"))
                report = page.evaluate("document.getElementById('sceneScriptReport').textContent")
                self.assertTrue(report.startswith("Created “The Harbour Light” with 9 lines.\nLeft out:\n"), report)
                self.assertIn("• Line 46 (McCLANE): its speaker is left out.", report)
                self.assertIn("• Line 31 (TOMAS): (beat)", report)

                page.set_value("#sceneScriptFormat", "text")
                page.set_value("#sceneScriptText", (scripts / "harbour.txt").read_text(encoding="utf-8"))
                page.click("#sceneScriptRead")
                page.wait_until("!document.getElementById('sceneScriptMatch').hidden")
                self.assertEqual(page.evaluate("document.getElementById('sceneScriptSummary').textContent"),
                                 "8 lines, 3 speakers, 1 line with no name, 2 lines not understood.")
                self.assertEqual(page.evaluate("[...document.querySelectorAll('#sceneScriptUnread li')].map((li) => li.textContent)"),
                                 ["Line 8: (They watch the water.) (a direction with no words to say)",
                                  "Line 11: Tomas: (a name with no words to say)"])
                page.set_value("#sceneScriptSpeakers tr:nth-child(3) select", "-")
                page.set_value("#sceneScriptName", "Harbour, plain text")
                page.click("#sceneScriptCreate")
                page.wait_until("document.getElementById('sceneName').value === 'Harbour, plain text'"
                                " && document.querySelectorAll('.scene-table tbody tr.scene-row').length === 6")
                characters = page.evaluate("[...document.querySelectorAll('.scene-table tbody tr.scene-row .scene-character')]"
                                           ".map((cell) => cell.textContent)")
                self.assertEqual(characters, ["Mara", "Tomas", "Mara", "Tomas", "Mara", "Mara"])
                self.assertEqual(len(server.scenes.list_scenes()), 2)
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class SamplePageTests(unittest.TestCase):
    """The sample scene from an empty Cast: opened, rendered, built and heard without typing a word, then removed, which leaves
    the library as it was."""

    def test_the_sample_is_heard_without_typing_and_removing_it_leaves_the_library_as_it_was(self):
        import kokoro_support
        from tests.test_sample_project import library_state

        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp, \
                mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: ["af_heart", "am_michael"]):
            root = Path(temp)
            server = FlowServer(root)
            server.engines.install_probe = lambda entry: {"installed": entry["id"] == "kokoro"}
            page = None
            rows = "document.querySelectorAll('#sceneLineEditor .scene-line[data-status=\"{}\"]').length === 9"
            try:
                self.assertEqual(server.cast.list_cast(), [])  # which also makes the Cast's database
                before = library_state(root)
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#cast")
                page.wait_until("!document.getElementById('castSampleNote').hidden")
                self.assertTrue(page.evaluate("$('sceneBatch').hidden && $('scenePlayers').hidden && $('sceneWork').hidden"))
                self.assertTrue(page.evaluate("document.getElementById('sampleNote').hidden === false"
                                              " && document.getElementById('sampleRemove').hidden"))
                self.assertEqual(server.cast.list_cast(), [])  # offered, never made unasked
                page.show("home")
                page.wait_until("!$('homeStart').hidden && !$('homeSampleLink').hidden")
                self.assertTrue(page.evaluate("$('homePerform').hidden"))
                page.click("#homeSampleOpen")
                page.wait_until("location.hash === '#scenes' && document.getElementById('sceneName').value === "
                                "'Sample: The parcel at the lighthouse' && document.querySelectorAll('#sceneLineEditor .scene-line')"
                                ".length === 9")
                page.wait_until("!document.getElementById('sampleRemove').hidden && document.getElementById('sampleOpen').hidden")
                self.assertTrue(page.evaluate("document.getElementById('sampleText').textContent")
                                .startswith("This is the sample scene"))
                self.assertTrue(page.evaluate("document.getElementById('castSampleNote').hidden"))
                page.click("#sceneStart")
                page.wait_until("!$('sceneBatch').hidden && $('scenePlayers').hidden")
                page.wait_until(rows.format("choose_take"))
                page.click("#sceneChooseNewest")
                page.wait_until(rows.format("done"))
                page.click("#scenePreview")
                page.wait_until("!$('scenePlayers').hidden && $('scenePreviewPlayer').getAttribute('src')")
                page.open(server.base + "/?reopen#scenes")
                page.wait_until("!$('scenePlayers').hidden && $('scenePreviewPlayer').getAttribute('src')")
                self.assertEqual(page.evaluate("fetch(document.getElementById('scenePreviewPlayer').getAttribute('src'))"
                                               ".then((r) => r.status)"), 200)
                self.assertEqual([request["voice"] for request in server.engines.requests[:2]], ["af_heart", "am_michael"])

                page.evaluate("window.confirm = (text) => { window.lastConfirm = text; return true; }")
                page.click("#sampleRemove")
                page.wait_until("document.getElementById('sampleStatus').textContent.startsWith('Removed the sample scene')")
                confirmed = page.evaluate("window.lastConfirm")
                self.assertIn("Sample: The parcel at the lighthouse", confirmed)
                self.assertIn("2 characters and their voices, 9 takes", confirmed)
                page.wait_until("!document.getElementById('sampleOpen').hidden && !document.getElementById('castSampleNote').hidden")
                self.assertEqual(library_state(root), before)
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class ModelHelpPageTests(unittest.TestCase):
    """The Models page answers "which model clones a voice on a computer with no graphics card, in Spanish?" in three clicks,
    from the real catalog's facts."""

    def test_three_clicks_find_the_model_that_clones_with_no_graphics_card_in_spanish(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            page = None
            shown = ("Array.from(document.querySelectorAll('#models .model-card')).filter((card) => card.getClientRects().length)"
                     ".map((card) => card.querySelector('h3').textContent)")
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#models")
                page.wait_until("document.querySelectorAll('#modelFilterLanguages input').length > 1"
                                " && !document.getElementById('modelFilter').hidden")
                self.assertEqual(page.evaluate("Array.from(document.querySelectorAll('.first-model'))"
                                               ".map((badge) => badge.closest('.model-card').querySelector('h3').textContent)"),
                                 ["Kokoro 82M"])
                page.click("#modelFilterCpu")
                page.click("#modelFilterClone")
                page.click('#modelFilterLanguages input[value="Spanish"]')
                drama = "Fish Audio Drama 3 (35B MoE / Q4_K)"
                page.wait_until(f"document.getElementById('modelFilterMatch').textContent === '1 model fits: {drama}.'")
                self.assertEqual(page.evaluate(shown), [drama])
                facts = page.evaluate("Array.from(document.querySelectorAll('#models .model-card')).find((card) => card.getClientRects()"
                                      ".length).querySelectorAll('.model-facts li').length")
                self.assertEqual(facts, 5)
                page.click('#modelFilterLanguages input[value=""]')
                page.click("#modelFilterClone")
                page.click("#modelFilterCpu")
                page.wait_until("document.getElementById('modelFilterMatch').hidden")
                self.assertEqual(page.evaluate("document.querySelectorAll('#models .model-card').length"), len(server.engines.catalog["engines"]))
                self.assertEqual(page.evaluate("document.querySelectorAll('#models .model-card[hidden]').length"), 0)
                self.assertEqual(page.evaluate("document.querySelectorAll('#models .model-support').length"),
                                 len(server.engines.catalog["engines"]))
                page.evaluate("document.getElementById('modelFilterClone').focus()")
                page.press("Tab", "Tab", 9)
                self.assertEqual(page.evaluate("document.activeElement.labels[0].textContent.trim()"),
                                 "Show only models tested for every release")
                page.press(" ", "Space", 32)
                names = [entry["name"] for entry in server.engines.catalog["engines"] if entry["support"] == "supported"]
                self.assertCountEqual(page.evaluate(shown), names)
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()

    def test_a_subtitle_file_uses_the_scene_import_and_cue_editor(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            page = None
            try:
                for name in ("Mira", "Tobin"):
                    character = server.cast.create_character(name, "")
                    identity = server.cast.create_identity(character["id"], "qwen3-tts-1.7b-base", name)
                    reference = server.cast.add_reference(identity["id"], f"{name}.wav", wav_bytes(),
                                                          transcript="A reference.", origin="test", allowed_use="tests")
                    server.cast.update_identity(identity["id"], {"status": "approved", "reference_version_id": reference["id"]})
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#scenes")
                page.wait_until("castCharacters.length === 2")
                page.set_files("#sceneImportFile", ROOT / "tests" / "fixtures" / "scripts" / "timed-harbour.srt")
                page.wait_until("!document.getElementById('sceneScriptCreate').disabled")
                self.assertEqual(page.evaluate("document.getElementById('sceneScriptFormat').value"), "srt")
                self.assertIn("overlaps", page.evaluate("document.getElementById('sceneScriptCues').textContent"))
                page.click("#sceneScriptCreate")
                page.wait_until("document.querySelectorAll('.scene-row').length === 4")
                page.click(".scene-row .scene-open")
                page.set_value('input[aria-label="Cue start for line 1"]', "1.05")
                page.set_value('input[aria-label="Cue end for line 1"]', "00:01.000")
                page.click("#sceneSave")
                page.wait_until("document.getElementById('sceneError').textContent.includes('Line 1 (00:01.050)')")
                self.assertNotIn("cue-001", page.evaluate("document.getElementById('sceneError').textContent"))
                page.set_value('input[aria-label="Cue end for line 1"]', "00:01.400")
                page.click("#sceneSave")
                page.wait_until("!sceneDirty && $('sceneStatus').textContent.includes(' / SHA-256 ')")
                saved = server.scenes.get_scene(server.scenes.list_scenes()[0]["scene_id"])
                self.assertEqual(saved["lines"][0]["cue"]["start_ms"], 1050)
                self.assertEqual(saved["lines"][0]["cue"]["source_start_ms"], 1000)
                if page.evaluate("document.querySelector('.scene-open').getAttribute('aria-expanded') === 'false'"):
                    page.click(".scene-row .scene-open")
                page.evaluate("document.querySelector('textarea[aria-label=\"Dialogue — line 1\"]').setSelectionRange(6, 6)")
                page.click('button[aria-label="Split cue at text cursor"]')
                page.wait_until("document.querySelectorAll('.scene-row').length === 5")
                page.click('button[aria-label="Merge with next cue"]')
                page.wait_until("document.querySelectorAll('.scene-row').length === 4")
                page.click("#sceneSave")
                page.wait_until("!sceneDirty && $('sceneStatus').textContent.includes(' / SHA-256 ')")
                reopened = server.scenes.get_scene(saved["scene_id"])
                first = reopened["lines"][0]
                self.assertNotEqual(first["line_id"], "cue-001")
                self.assertEqual(first["cue"]["source_line_ids"], ["cue-001"])
                self.assertIn("cue-001", first["cue"]["replaces"])
                self.assertEqual((first["cue"]["start_ms"], first["cue"]["end_ms"]), (1050, 1400))
                self.assertEqual(reopened["timeline"]["source_cues"], saved["timeline"]["source_cues"])
                page.click("#sceneStart")
                page.wait_until("document.querySelectorAll('.scene-line[data-status=\"choose_take\"]').length === 4")
                page.click("#sceneChooseNewest")
                page.wait_until("document.querySelectorAll('.scene-line[data-status=\"done\"]').length === 4")
                self.assertIn("Too long by 0.2 s", page.evaluate("document.querySelector('.scene-status').textContent"))
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()

    def test_scene_refresh_preserves_an_edit_made_after_saving(self):
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            server = FlowServer(Path(temp))
            page = None
            try:
                character = server.cast.create_character("Mira", "")
                voice = server.cast.create_identity(character["id"], "kokoro", "Warm", {"voice": "af_heart"})
                server.cast.update_identity(voice["id"], {"status": "approved"})
                server.scenes.import_script(
                    "Mira: Keep the door open.", "text", {"Mira": {"identity_id": voice["id"]}}, name="Before saving"
                )
                scene_id = server.scenes.list_scenes()[0]["scene_id"]
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#scenes")
                page.wait_until("$('sceneName').value === 'Before saving'")
                page.evaluate(f"""(() => {{
                    const originalFetch = fetchJson;
                    const originalRefresh = refreshSceneList;
                    fetchJson = async function (path, body) {{
                        const result = await originalFetch(path, body);
                        if (!body && path === {json.dumps("/api/scenes/" + scene_id)} && !window.releaseSceneRead) {{
                            await new Promise(resolve => {{ window.releaseSceneRead = resolve; }});
                        }}
                        return result;
                    }};
                    refreshSceneList = function (...args) {{
                        window.finishedSceneRefresh = originalRefresh(...args);
                        return window.finishedSceneRefresh;
                    }};
                }})()""")
                page.set_value("#sceneName", "First edit")
                page.click("#sceneSave")
                page.wait_until("!!window.releaseSceneRead")
                self.assertEqual(server.scenes.get_scene(scene_id)["name"], "First edit")
                page.set_value("#sceneName", "Second edit")
                page.evaluate("(async () => { window.releaseSceneRead(); await window.finishedSceneRefresh; })()")
                self.assertEqual(page.evaluate("$('sceneName').value"), "Second edit")
                self.assertTrue(page.evaluate("sceneDirty"))
                self.assertEqual(page.evaluate("$('sceneSelect').value"), scene_id)
                page.click("#sceneSave")
                page.wait_until("!sceneDirty && $('sceneStatus').textContent.startsWith('Second edit / revision ')")
                self.assertEqual(server.scenes.get_scene(scene_id)["name"], "Second edit")
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class ConnectedAppsPageTests(unittest.TestCase):
    def test_system_shows_verified_apps_the_slate_count_and_gpu_details(self):
        import fels_suite
        from tests.test_connect import stand_in_app
        from tests.test_inbox import contract_clip

        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True)))
        environ = {"FELS_SUITE_DIR": str(root / "suite"), "NPCFORGE_URL": "", "ALTERA_URL": ""}
        self.enterContext(mock.patch.dict(os.environ, environ))
        # All test services listen on free ports. Keep any apps running on this computer out of the test.
        self.enterContext(mock.patch.dict(fels_suite.APPS, {
            app: {**fels_suite.APPS[app], "port": None, "old_ports": ()} for app in ("npc-forge", "altera")
        }))
        server = FlowServer(root)
        self.addCleanup(server.close)
        forge = stand_in_app(self, {"fels": fels_suite.health_block("npc-forge", "1.1.2", 45), "online": False})
        altera = stand_in_app(self, {"fels": fels_suite.health_block("altera", "1.0.1", 1)})
        fels_suite.write_address("npc-forge", forge, "1.1.0", 45, "forge", environ)
        fels_suite.write_address("altera", altera, "1.0.0", 1, "altera", environ)
        fels_suite.inbox_dir("voice-lab", environ, make=True)
        fels_suite.inbox_write("voice-lab", "voice-slate", "Maya.voiceslate", contract_clip(), environ)
        forge_gpu = {"app": "npc-forge", "name": "NPC Forge", "pid": os.getpid(), "what": "pictures",
                     "since": "2026-11-23T16:10:00Z", "alone": True}
        server.engines.gpu_state = lambda: {"device": "cpu", "held": False, "holders": [forge_gpu], "alone": forge_gpu}

        page = DevToolsPage(BROWSER)
        self.addCleanup(page.close)
        page.open(server.base + "/#system")
        page.wait_until("document.getElementById('forgeConnection').textContent.includes('1.1.2')")
        self.assertEqual(page.evaluate("document.getElementById('forgeAddress').textContent"), forge)
        self.assertTrue(page.evaluate("document.getElementById('forgeSetup').hidden"))
        self.assertIn("1.0.1", page.evaluate("document.getElementById('alteraConnection').textContent"))
        self.assertEqual(page.evaluate("document.getElementById('alteraAddress').textContent"), altera)
        page.wait_until("document.getElementById('connectedSlateCount').textContent === '1 clip waiting.'")
        page.wait_until("document.getElementById('connectedGpuState').textContent.includes('NPC Forge, alone: pictures')")

        page.evaluate("document.querySelector('#connectedGpu summary').focus()")
        page.press(" ", "Space", 32)
        self.assertTrue(page.evaluate("document.getElementById('connectedGpu').open"))
        self.assertIn("since", page.evaluate("document.getElementById('connectedGpuState').textContent"))
        self.assertTrue(page.evaluate("document.getElementById('connectSettings').matches(':not([open])')"))
        page.screenshot("connected-apps", "#connectedApps")

        fels_suite.remove_address("npc-forge", environ)
        # A live address that really serves a different app must not be displayed as Altera.
        fels_suite.write_address("altera", server.base, "1.0.0", 1, "altera", environ)
        page.evaluate("document.getElementById('connectedAppsRefresh').focus()")
        page.press(" ", "Space", 32)
        page.wait_until("!document.getElementById('forgeSetup').hidden && !document.getElementById('alteraSetup').hidden")
        self.assertTrue(page.evaluate("document.getElementById('forgeAddress').hidden && document.getElementById('alteraAddress').hidden"))
        for app, words in (("forge", "game character dialogue"), ("altera", "spoken replies from your companions")):
            self.assertIn(words, page.evaluate(f"document.getElementById('{app}Connection').parentElement.textContent"))
            self.assertIn("Start the app", page.evaluate(f"document.getElementById('{app}Connection').textContent"))
            self.assertIn("https://github.com/Fel101/", page.evaluate(f"document.getElementById('{app}Setup').href"))
        server.engines.gpu_state = lambda: {"device": "cpu", "held": False, "holders": [], "alone": None}
        page.wait_until("document.getElementById('connectedGpuState').textContent.includes('runs on the CPU')")
        page.screenshot("connected-apps-offline", "#connectedApps")
        page.click('#connectedApps a[href="#cast"]')
        page.wait_until("!document.getElementById('slateInbox').hidden && window.location.hash === '#cast'")
        self.assertEqual(page.problems, [])


@unittest.skipUnless(BROWSER, "needs headless Microsoft Edge or Google Chrome")
class InboxPageTests(unittest.TestCase):
    """Voice Slate's Send to Voice Lab puts a clip in the suite's inbox (contract 6). The Cast page shows it within ten seconds
    of getting focus and imports it only when the person presses Import; a refused file can only be dismissed."""

    def test_a_clip_in_the_inbox_shows_on_the_cast_page_and_imports_as_a_candidate(self):
        import datetime

        import fels_suite
        from slate_inbox import SlateInbox
        from tests.test_inbox import contract_clip

        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            environ = {"FELS_SUITE_DIR": str(root / "suite")}
            fels_suite.inbox_dir("voice-lab", environ, make=True)
            cleared = []
            server = FlowServer(root)
            server.server.RequestHandlerClass.lab.inbox = SlateInbox(environ, discard=cleared.append)
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/#cast")
                page.wait_until("document.getElementById('slateInbox').hidden && document.getElementById('inboxNavCount').hidden")
                time.sleep(1)  # the page has looked once; the next look waits for its ten seconds

                raw = contract_clip()
                sent = datetime.datetime(2026, 12, 1, 10, 2, 11, tzinfo=datetime.timezone.utc)
                clip = fels_suite.inbox_write("voice-lab", "voice-slate", "Maya, take 3.voiceslate", raw, environ, now=sent)
                wrong = clip.with_name("20261201T100500Z-voice-slate-song.mp3")
                wrong.write_bytes(b"ID3")
                focused = time.monotonic()
                page.evaluate("window.dispatchEvent(new Event('focus'))")
                page.wait_until("document.querySelectorAll('#slateInboxList li').length === 2", timeout=15)
                self.assertLess(time.monotonic() - focused, 10.5)
                # A file that is not a clip turns "clips from Voice Slate" into "files"; the menu says how many wait.
                self.assertEqual(page.evaluate("document.getElementById('slateInboxTitle').textContent"),
                                 "2 files waiting in the inbox")
                self.assertEqual(page.evaluate("document.getElementById('inboxNavCount').textContent"),
                                 "2, 2 files waiting in the inbox")
                self.assertEqual(page.evaluate("document.getElementById('connectedSlateCount').textContent"), "1 clip waiting.")
                item = f'#slateInboxList li[data-name="{clip.name}"]'
                refused = f'#slateInboxList li[data-name="{wrong.name}"]'
                self.assertIn("The lantern swung once", page.evaluate(f"document.querySelector('{item}').textContent"))
                self.assertEqual(page.evaluate(f"[...document.querySelectorAll('{refused} button')].map((b) => b.textContent)"),
                                 ["Dismiss"])
                self.assertEqual(page.evaluate(f"document.querySelector('{refused} strong').textContent"), "song.mp3")
                self.assertIn("only .voiceslate files", page.evaluate(f"document.querySelector('{refused}').textContent"))

                # Preview opens the import form under the clip, with the speaker's name to start from. Nothing is in Cast yet.
                page.click(f"{item} button")
                page.wait_until(f"document.getElementById('slateImportForm').closest('li') === document.querySelector('{item}')")
                self.assertEqual(page.evaluate("document.activeElement.id"), "slateCharacter")
                self.assertEqual(page.evaluate("document.getElementById('slateCharacter').value"), "maya")
                self.assertEqual(page.evaluate(f"document.querySelector('{item} button').getAttribute('aria-expanded')"), "true")
                self.assertEqual(server.cast.list_cast(), [])
                page.set_value("#slateCharacter", "Maya")
                page.click("#slateImportGo")
                page.wait_until("document.getElementById('slateInboxStatus').textContent.startsWith('The clip is in Cast.')")
                (character,) = server.cast.list_cast()
                self.assertEqual((character["name"], character["identities"][0]["status"]), ("Maya", "candidate"))
                self.assertEqual(cleared, [str(clip)])
                self.assertEqual(page.evaluate("document.querySelectorAll('#slateInboxList li').length"), 1)
                self.assertEqual(page.evaluate("document.getElementById('slateImportForm').closest('li')"), None)
                self.assertEqual(page.evaluate("document.getElementById('slateInboxTitle').textContent"),
                                 "1 file waiting in the inbox")
                self.assertEqual(page.evaluate("document.getElementById('connectedSlateCount').textContent"), "0 clips waiting.")

                page.evaluate(f"document.querySelector('{refused} button').focus()")  # as a real click or the keyboard leaves it
                page.click(f"{refused} button")
                page.wait_until("document.getElementById('slateInbox').hidden && document.getElementById('inboxNavCount').hidden")
                self.assertEqual(cleared, [str(clip), str(wrong)])
                self.assertIn("Recycle Bin", page.evaluate("document.getElementById('slateInboxStatus').textContent"))
                self.assertEqual(page.evaluate("document.activeElement.id"), "castTitle")

                # The file box still previews and imports a picked file: the same clip again changes nothing.
                picked = root / "picked.voiceslate"
                picked.write_bytes(raw)
                page.evaluate("document.querySelector('#cast details:has(#slateBundle)').open = true")
                page.set_files("#slateBundle", picked)
                page.wait_until("!document.getElementById('slateImportForm').hidden")
                self.assertEqual(page.evaluate("document.getElementById('slateImportForm').parentElement.id"), "slateFormHome")
                page.set_value("#slateCharacter", "Maya")
                page.click("#slateImportGo")
                page.wait_until("document.getElementById('slateImportStatus').textContent.startsWith('This clip is already in Cast.')")
                self.assertEqual(len(server.cast.list_cast()), 1)
                self.assertEqual(page.problems, [])
            finally:
                if page is not None:
                    page.close()
                server.close()
