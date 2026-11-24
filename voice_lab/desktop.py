"""Fel's Voice Lab desktop window. Own only the service started by this launch.

The window's service listens on the known port, 8765, unless desktop.local.json's "port" says another (0 is any free port), so
other programs on this computer find it (lab_address.py). When this Lab's own service already answers there, started by
serve.bat, the window opens on it instead of starting a second one, and leaves it running when it closes."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time

from voice_lab import desktop_lifecycle
from voice_lab import lab_address
from voice_lab.local_ownership import LocalOwnership
from voice_lab import self_update

ROOT = Path(__file__).resolve().parents[1]


class DesktopBackend:
    """The service one desktop window owns. Its session folder goes in `sessions` (out/desktop by default), and
    `service_args` are added to the service's command line; the tests use both to keep a run out of the real out/ folder."""

    STOP_WAIT = 30  # seconds the service has to stop when the window closes, unless it is finishing a package command

    def __init__(self, *, root=ROOT, state_root=None, python=None, device="auto", sessions=None, service_args=(), port=0):
        self.root = Path(root).resolve()
        self.state_root = Path(state_root or root).resolve()
        self.python = Path(python or self.root / ".venv/Scripts/python.exe").resolve()
        self.device = device
        self.sessions = Path(sessions or self.root / "out/desktop")
        self.service_args = [str(arg) for arg in service_args]
        self.process = None
        self.job = None
        self.log = None
        self.session = None
        self.nonce = secrets.token_hex(32)
        self.url = None
        # The port the service asks for (0: any free port); serve.py takes a free one when another program has the known
        # port. joined: the window shows a service it did not start; joined_stopped: that service went away.
        self.port = port
        self.joined = False
        self.joined_stopped = False
        self.closing = threading.Event()  # set by close(), so the watch of a joined service ends with the window

    def find_running(self, ask=lab_address.ask_health):
        """The address of this Lab's own service when it already answers on the window's port, as when serve.bat started
        it, or None. A service of another Lab folder or library is not this window's to show."""
        if not self.port:
            return None
        url = f"http://127.0.0.1:{self.port}"
        answer = ask(url)
        if lab_address.is_lab(answer) and answer.get("lab_id") == lab_address.lab_id(self.root, self.state_root):
            return url
        return None

    def start(self, timeout=90):
        running = self.find_running()
        if running:
            logging.info("This Lab's service already runs at %s; the window opens on it", running)
            self.joined, self.url = True, running
            return running
        if not self.python.is_file():
            raise RuntimeError("The backend Python is missing. Run setup.bat first.")
        from voice_lab.desktop_process import OwnedJob
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.session = Path(tempfile.mkdtemp(prefix="session-", dir=self.sessions))
        (self.session / "request.json").write_text(json.dumps({"nonce": self.nonce}), "utf-8")
        self.log = (self.session / "backend.log").open("w", encoding="utf-8")
        self.job = OwnedJob()
        # The gate holds the child before it can create any descendant processes.
        gate = "import sys; permit=sys.stdin.buffer.read(1); permit==b'1' or sys.exit(1); import runpy; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        command = [str(self.python), "-X", "utf8", "-u", "-c", gate,
                   str(self.root / "serve.py"), "--port", str(self.port), "--device", self.device,
                   "--state-root", str(self.state_root), "--desktop-session", str(self.session),
                   "--parent-pid", str(os.getpid()), *self.service_args]
        try:
            self.process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.PIPE,
                stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.job.add(self.process)
            self.process.stdin.write(b"1")
            self.process.stdin.close()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Fel's Voice Lab could not start. See {self.session / 'backend.log'}")
                ready = self.session / "ready.json"
                if ready.is_file():
                    payload = json.loads(ready.read_text("utf-8"))
                    # Windows venv launchers can have a separate interpreter PID.
                    if payload.get("nonce") != self.nonce:
                        raise RuntimeError("Desktop session identity mismatch")
                    from urllib.parse import urlparse
                    address = urlparse(payload["url"])
                    if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port:
                        raise RuntimeError("Invalid desktop service address")
                    self.url = payload["url"]
                    return self.url
                time.sleep(.1)
            raise TimeoutError(f"Fel's Voice Lab startup timed out. See {self.session / 'backend.log'}")
        except BaseException:
            self.close()
            raise

    def wait_for_update(self, close_window, ask=lab_address.ask_health, pause=2.0):
        """Close the window when the service has stopped for Update now. The service's own update.json in the session
        folder says so; a service that stops for any other reason leaves the window as it is. A service the window joined
        is watched instead: when it stops answering (serve.bat was closed, or it stopped for Update now), the window closes
        too, and main says why."""
        if self.joined:
            misses = 0
            while misses < 3:
                if self.closing.wait(pause):
                    return
                misses = 0 if ask(self.url) else misses + 1
            logging.info("The service at %s stopped answering; closing the window", self.url)
            self.joined_stopped = True
            close_window()
            return
        if self.process is None:
            return
        self.process.wait()
        if self_update.requested(self.session):
            logging.info("The service stopped for Update now; closing the window")
            close_window()

    def hand_over_update(self, starter=subprocess.Popen):
        """Once the window has closed and its service has ended: start the updater the service asked for. It runs outside
        this window's job, so it outlives the window, and it opens the window again when it is done."""
        if not self_update.requested(self.session):
            return False
        logging.info("Starting the updater")
        self_update.start_updater(self.root, "window", starter=starter)
        return True

    def close(self):
        self.closing.set()
        try:
            if self.process and self.process.poll() is None:
                if self.session:
                    (self.session / "stop").write_text(self.nonce, "utf-8")
                try:
                    self.process.wait(timeout=self.STOP_WAIT)
                except subprocess.TimeoutExpired:
                    installing = desktop_lifecycle.finishing(self.session) if self.session else None
                    if installing is not None:
                        # Ending the service now would end pip halfway and could leave the model's environment broken.
                        logging.info("Waiting for the service to finish the package command of the %s install", installing)
                        self.process.wait()
                    else:
                        logging.warning("Backend did not finish shutdown; stopping its owned process tree")
        finally:
            if self.job:
                self.job.close()
                self.job = None
            if self.process:
                self.process.wait(timeout=10)
            if self.log:
                self.log.close()
                self.log = None


def show_error(message, icon=0x10):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), "Fel's Voice Lab", icon)
    else:
        print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--smoke-seconds", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise RuntimeError("The desktop launcher currently supports Windows 10/11 only")
    settings_path = ROOT / "desktop.local.json"
    settings = json.loads(settings_path.read_text("utf-8")) if settings_path.is_file() else {}
    state_root = args.state_root or ROOT / settings.get("state_root", ".")
    owner = LocalOwnership(ROOT / "out/desktop/instance.lock")
    if not owner.acquire():
        show_error("Fel's Voice Lab is already open. Switch to its existing window.")
        return 0
    logs = ROOT / "out/desktop"
    logging.basicConfig(filename=str(logs / "launcher.log"), encoding="utf-8", level=logging.INFO)
    backend = DesktopBackend(state_root=state_root, device=args.device, port=lab_address.window_port(ROOT))
    try:
        import webview
        # Downloads remain ordinary local exports; no Python API is exposed to the page.
        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window("Fel's Voice Lab", backend.start(), width=1440, height=960,
                                      min_size=(960, 650), background_color="#121212")    # the neutral Dark background, until the page paints
        def started():
            if args.smoke_seconds is not None:
                time.sleep(args.smoke_seconds)
                window.destroy()
                return
            backend.wait_for_update(window.destroy)
        webview.start(started, gui="edgechromium", private_mode=True,
                      icon=str(ROOT / "assets/voice-lab.ico"))
    finally:
        try:
            backend.close()
        finally:
            owner.release()
    backend.hand_over_update()
    if backend.joined_stopped:
        show_error(f"The Lab's service at {backend.url} has stopped, so its window has closed. Open Fel's Voice Lab again "
                   "to start it.", icon=0x40)
    return 0


def launch():
    try:
        sys.exit(main())
    except Exception as error:
        logging.exception("Desktop launch failed")
        show_error(f"{error}\n\nRun setup-desktop.bat if the desktop environment is missing.\nLogs: {ROOT / 'out/desktop'}")
        sys.exit(1)


if __name__ == "__main__":
    launch()
