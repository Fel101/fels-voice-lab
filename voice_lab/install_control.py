"""Coordinate Stop between the service and a guided model installer."""

import os
import subprocess
import time
from pathlib import Path

CONTROL_ENV = "VOICE_LAB_INSTALL_CONTROL"
STOPPED = 125


class InstallControl:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.phase_path = self.folder / "phase"
        self.stop_path = self.folder / "stop"

    def current_phase(self):
        for _ in range(5):
            try:
                return self.phase_path.read_text(encoding="ascii")
            except FileNotFoundError:
                return "preparing"
            except PermissionError:  # Windows can deny a read while the installer replaces the file.
                time.sleep(0.05)
        return "waiting"

    def request_stop(self):
        self.stop_path.touch()
        return self.current_phase()

    def enter(self, phase):
        # Publish the phase before checking the stop flag. The service writes the flag before it reads
        # the phase, so it can never decide to kill a process that is about to start changing packages.
        temporary = self.phase_path.with_suffix(".tmp")
        temporary.write_text(phase, encoding="ascii")
        for attempt in range(5):
            try:
                temporary.replace(self.phase_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
        if self.stop_path.exists():
            raise SystemExit(STOPPED)


def phase(name):
    folder = os.environ.get(CONTROL_ENV)
    if folder:
        InstallControl(folder).enter(name)


def package_step(command, *, runner=subprocess.run, phase_name="packages", **kwargs):
    """Finish a single venv or pip command before honoring Stop."""
    phase(phase_name)
    result = runner(command, **kwargs)
    if result.returncode == 0:
        phase("preparing")
    return result
