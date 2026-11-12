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
              "exportError", "exportStatus"]


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
        loaded once more; one that never gets ready is reported with what the browser was showing."""
        self.failed, self.requests = [], {}
        self.call("Network.enable")  # only while the page loads: it is what says which request failed
        notes = []
        try:
            for attempt, seconds in enumerate(patience, start=1):
                started = self.call("Page.navigate", {"url": url}) if attempt == 1 else self.load_again(url)
                try:
                    return self.wait_until("document.documentElement.classList.contains('workspace-ready')", timeout=seconds)
                except AssertionError:
                    notes.append(f"load {attempt}, after {seconds} s: the browser answered {started}\n{self.look()}")
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
        self.evaluate(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('no element {selector}');
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})()""")

    def choose(self, selector, text):
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
    """The real request handler over temporary stores, with the fake worker and a running batch runner."""

    def __init__(self, root):
        self.cast = CastStore(root)
        self.engines = BrowserFakeEngines()
        self.server = serve.LocalThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.scenes = SceneStore(self.cast, self.engines.entries, root=root)
        self.runner = SceneBatchRunner(self.scenes, self.base)
        evaluations = EvaluationStore(root / "data" / "evaluations" / "evaluations.sqlite3", lab_root=root)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, port, self.cast, self.scenes,
                                                             self.runner, evaluations)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.runner.start()

    def close(self):
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
        page.failed, page.requests, page.asked = [], {}, []
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
                page.set_value("#referenceAllowedUse", "Tests only")
                page.wait_until("!document.getElementById('saveReference').disabled")
                page.click("#saveReference")
                # The save message is replaced when the cast refreshes, so wait for the version in the approval list.
                page.wait_until("document.getElementById('approveReference').options.length > 0")
                page.click("#approveIdentity")
                page.wait_until("document.getElementById('identityState').textContent.includes('approved reference v1')")

                # Perform: a set of two takes of one line from the approved voice, made one after the other.
                page.show("perform")
                page.choose("#performCharacter", "Mira")
                page.wait_until("document.getElementById('performIdentity').value !== ''")
                self.assertTrue(page.evaluate("!document.getElementById('variantCount').parentElement.hidden"))
                page.set_value("#variantCount", "2")
                page.click("#performGenerate")
                page.wait_until("document.querySelectorAll('#takeList audio').length >= 2")
                page.wait_until("document.getElementById('performStatus').textContent.startsWith('Candidate set complete')")
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
class GenerateAfterInstallTests(unittest.TestCase):
    def test_a_page_opened_before_kokoro_was_installed_speaks_once_it_is(self):
        installed = []  # the Kokoro voices on disk: none while the page opens, two once the installer has run
        with tempfile.TemporaryDirectory(prefix="voice-lab-browser-", ignore_cleanup_errors=True) as temp, \
                mock.patch.object(serve, "discover_kokoro_voices", lambda: list(installed)):
            server = FlowServer(Path(temp))
            page = None
            try:
                page = DevToolsPage(BROWSER)
                page.open(server.base + "/")
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
