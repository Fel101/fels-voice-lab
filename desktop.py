"""Fel's Voice Lab desktop window. Own only the service started by this launch."""
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
import time

from local_ownership import LocalOwnership

ROOT = Path(__file__).resolve().parent


class DesktopBackend:
    def __init__(self, *, root=ROOT, state_root=None, python=None, device="auto"):
        self.root = Path(root).resolve()
        self.state_root = Path(state_root or root).resolve()
        self.python = Path(python or self.root / ".venv/Scripts/python.exe").resolve()
        self.device = device
        self.process = None
        self.job = None
        self.log = None
        self.session = None
        self.nonce = secrets.token_hex(32)
        self.url = None

    def start(self, timeout=90):
        if not self.python.is_file():
            raise RuntimeError("The backend Python is missing. Run setup.bat first.")
        from desktop_process import OwnedJob
        logs = self.root / "out/desktop"
        logs.mkdir(parents=True, exist_ok=True)
        self.session = Path(tempfile.mkdtemp(prefix="session-", dir=logs))
        (self.session / "request.json").write_text(json.dumps({"nonce": self.nonce}), "utf-8")
        self.log = (self.session / "backend.log").open("w", encoding="utf-8")
        self.job = OwnedJob()
        # The gate holds the child before it can create any descendant processes.
        gate = "import sys; permit=sys.stdin.buffer.read(1); permit==b'1' or sys.exit(1); import runpy; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        command = [str(self.python), "-X", "utf8", "-u", "-c", gate,
                   str(self.root / "serve.py"), "--port", "0", "--device", self.device,
                   "--state-root", str(self.state_root), "--desktop-session", str(self.session),
                   "--parent-pid", str(os.getpid())]
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

    def close(self):
        try:
            if self.process and self.process.poll() is None:
                if self.session:
                    (self.session / "stop").write_text(self.nonce, "utf-8")
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
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


def show_error(message):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), "Fel's Voice Lab", 0x10)
    else:
        print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--device", choices=("auto", "cpu"), default="auto")
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
    backend = DesktopBackend(state_root=state_root, device=args.device)
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
        webview.start(started, gui="edgechromium", private_mode=True,
                      icon=str(ROOT / "assets/voice-lab.ico"))
    finally:
        try:
            backend.close()
        finally:
            owner.release()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        logging.exception("Desktop launch failed")
        show_error(f"{error}\n\nRun setup-desktop.bat if the desktop environment is missing.\nLogs: {ROOT / 'out/desktop'}")
        sys.exit(1)
