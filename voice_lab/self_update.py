"""Update now: the System page's button. The update is tools/update.py's, the same as update.bat's; this is how the running
Lab hands it over.

The plan comes from `update.py --dry-run --json`, so the page shows what update.bat would. On yes the Lab closes and an
updater of its own, `update.py --from-lab` in a window of its own, waits for the Lab's programs to end, updates, and opens the
Lab again. A service the desktop window started cannot start that updater itself: the window keeps the service and everything
it starts in a Windows job that ends them all when the window closes. So the service leaves a request in the window's session
folder and stops, and the window starts the updater once it has closed (desktop.py). A service started by serve.bat starts
the updater itself, which then says to start the Lab again. After the update the new version's `update.py --finish` leaves a
notice (out\\update\\notice.json) that the page shows once.

Standard library only: desktop.py, in the window's own environment, uses updater_command too.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

PLAN_SECONDS = 300
NOTICE = Path("out") / "update" / "notice.json"
REQUEST = "update.json"  # in the desktop window's session folder: the window starts the updater when it has closed
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


CLOSING_FOR_CONSOLE = (
    "The Lab is closing for Update now. A window of its own shows the update; when it has finished, start the Lab again "
    "with serve.bat."
)


def say(text):
    print(text, flush=True)


class UpdateNowError(Exception):
    """Update now cannot go on; the message says why and what to do."""


def updater_command(root, reopen):
    """The updater: the Lab's own Python running update.py --from-lab. `reopen` is "window" to open the Lab's window again
    when it is done, or "none" to say how to start the Lab."""
    root = Path(root)
    return [str(root / ".venv" / "Scripts" / "python.exe"), str(root / "tools" / "update.py"), "--from-lab", "--reopen", reopen]


def start_updater(root, reopen, *, starter=subprocess.Popen):
    """Start the updater in a console window of its own, which stays when whoever started it ends."""
    return starter(updater_command(root, reopen), cwd=str(root), close_fds=True, creationflags=NEW_CONSOLE)


def requested(session):
    """Whether the service of this desktop session asked for the updater (desktop.py asks once the service has ended)."""
    return session is not None and (Path(session) / REQUEST).is_file()


class SelfUpdate:
    """What the service does for Update now. `desktop_session` is the desktop window's session folder when the window started
    this service (serve.main sets it), else None. The tests give `runner` and `starter` stand-ins for update.py and the
    updater's window, and `say` for the service's console."""

    def __init__(self, root, *, python=None, runner=subprocess.run, starter=subprocess.Popen, say=say):
        self.root = Path(root)
        self.python = Path(python or sys.executable)
        self.runner, self.starter, self.say = runner, starter, say
        self.desktop_session = None
        self._lock = threading.Lock()
        self.started = False

    @property
    def how(self):
        """How the Lab comes back: "window" (it opens again by itself) or "console" (start it again with serve.bat)."""
        return "window" if self.desktop_session else "console"

    def plan(self):
        """update.py's plan, as it would show it in update.bat: {"ok", "newer", "message", "from", "to", "release", "steps",
        "notes", "method"}, or {"ok": false, "problem"}."""
        command = [str(self.python), str(self.root / "tools" / "update.py"), "--dry-run", "--json"]
        try:
            done = self.runner(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PLAN_SECONDS,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpdateNowError(
                f"Reading the update took longer than {PLAN_SECONDS // 60} minutes, so it was stopped. Try again later."
            ) from exc
        except OSError as exc:
            raise UpdateNowError(f"The updater could not start ({exc}). Run update.bat in the Lab's folder instead.") from exc
        lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
        try:
            report = json.loads(lines[-1])
        except (IndexError, ValueError) as exc:
            tail = " ".join((done.stderr or "").strip().splitlines()[-3:])
            raise UpdateNowError(
                f"The updater did not say what it would do (exit code {done.returncode}). {tail} "
                "Run update.bat in the Lab's folder to see the whole message."
            ) from exc
        if not isinstance(report, dict) or "ok" not in report:
            raise UpdateNowError("The updater's answer could not be read. Run update.bat in the Lab's folder instead.")
        return report

    def start(self, stop):
        """Hand over to the updater and stop the service: `stop` ends it, and is called a moment after this returns, so the
        page gets its answer first."""
        with self._lock:
            if self.started:
                raise UpdateNowError("The update has already started. The Lab is closing.")
            if self.desktop_session:
                (Path(self.desktop_session) / REQUEST).write_text(json.dumps({"reopen": "window"}), encoding="utf-8")
            else:
                try:
                    start_updater(self.root, "none", starter=self.starter)
                except OSError as exc:
                    raise UpdateNowError(
                        f"The updater could not start ({exc}). Close the Lab and run update.bat in its folder instead."
                    ) from exc
                self.say(CLOSING_FOR_CONSOLE)  # serve.bat's console stays open at its pause, so it says why
            self.started = True
        timer = threading.Timer(0.5, stop)
        timer.daemon = True
        timer.start()
        return {"how": self.how}

    def notice(self):
        """What the last update, undo or Update now left for the page, or None."""
        try:
            notice = json.loads((self.root / NOTICE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return notice if isinstance(notice, dict) and notice.get("schema") == 1 and notice.get("state") else None

    def dismiss(self):
        try:
            (self.root / NOTICE).unlink()
        except FileNotFoundError:
            pass

    def open_folder(self):
        """Show the Lab's folder in File Explorer. Setup.exe puts it under the hidden AppData folder."""
        if os.name != "nt":
            raise UpdateNowError("Opening the folder works on Windows only.")
        os.startfile(str(self.root))  # noqa: S606 - the Lab's own folder, never a path from the request
        return {"folder": str(self.root)}
