"""Coordinate Stop between the service and a guided model installer."""

import os
import random
import subprocess
import time
from pathlib import Path

CONTROL_ENV = "VOICE_LAB_INSTALL_CONTROL"
STOPPED = 125
# Windows refuses to replace the phase file while another program has it open: the service reading it for the page, or a
# virus scanner looking at the new file. The installer tries again for this long before it gives up.
BUSY_SECONDS = 3.0


def _pause(attempt):
    """Wait a little longer after each refusal, by a random amount, so two programs that read and write the file on their
    own timers cannot stay in step and meet at every try (five tries 50 ms apart did on a GitHub runner)."""
    time.sleep(min(0.25, 0.01 * 2**attempt) * random.uniform(0.5, 1.5))


class InstallControl:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.phase_path = self.folder / "phase"
        self.stop_path = self.folder / "stop"

    def current_phase(self):
        for attempt in range(5):
            try:
                return self.phase_path.read_text(encoding="ascii")
            except FileNotFoundError:
                return "preparing"
            except PermissionError:  # Windows can deny a read while the installer replaces the file.
                _pause(attempt)
        return "waiting"

    def request_stop(self):
        self.stop_path.touch()
        return self.current_phase()

    def enter(self, phase):
        # Publish the phase before checking the stop flag. The service writes the flag before it reads
        # the phase, so it can never decide to kill a process that is about to start changing packages.
        temporary = self.phase_path.with_suffix(".tmp")
        temporary.write_text(phase, encoding="ascii")
        deadline = time.monotonic() + BUSY_SECONDS
        attempt = 0
        while True:
            try:
                temporary.replace(self.phase_path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                _pause(attempt)
                attempt += 1
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
