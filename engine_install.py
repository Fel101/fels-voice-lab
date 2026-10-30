"""Install an engine from the Models page: show the installer's plan, then run it and keep its progress.

An engine with a guided installer (Kokoro: tools/install_kokoro.py, the script setup-kokoro.bat runs) can be installed from its
card. The page first asks for the plan, which is the script's own --dry-run, so it shows what the script will really do and why
it would refuse. Install now then starts the script with --yes as a child process under the engine's own Python and keeps the end
of its output for the page to show. One install runs at a time. While it runs it holds the pool's job lock, so tests, loads and
clips are refused instead of competing with it for memory and disk. Closing the service stops the install and its children; the
script continues where it stopped when it is run again.
"""

import codecs
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import worker_pool

# Engine id -> the script its launcher runs (setup-kokoro.bat runs tools\install_kokoro.py). tests/test_engine_install.py keeps
# this in step with engine_checks.GUIDED_INSTALLERS and with the launchers themselves.
INSTALLERS = {"kokoro": "tools/install_kokoro.py"}
LOG_FOLDER = "out/install"
LOGS_KEPT = 10          # per engine; the newest runs' full output stays on disk
KEPT_LINES = 200        # lines of output held in memory
TAIL_LINES = 40         # lines a snapshot hands to the page
PLAN_TIMEOUT = 120
CLOSE_WAIT = 10
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DRY_RUN_DONE = "Checks passed."
_STEP = re.compile(r"(?:\d+\.|-)\s")
_TERMINATORS = re.compile(r"(\r\n|\n|\r)")


def paragraphs(text):
    """A script's hard-wrapped text as paragraphs: a blank line ends one, and a numbered or dashed line starts the next."""
    found, current = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current:
                found.append(" ".join(current))
                current = []
        elif current and _STEP.match(line):
            found.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        found.append(" ".join(current))
    return found


def _environment():
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


class EngineInstaller:
    """Plans and runs the guided installers for the Models page, one install at a time (see the note above)."""

    def __init__(self, pool, *, root=None, python=None, scripts=None):
        self.pool = pool
        self.root = Path(root or worker_pool.LAB)
        self.python = python    # None: the Python of the engine's own environment, as its launcher uses
        self.scripts = dict(INSTALLERS if scripts is None else scripts)
        self._lock = threading.Lock()
        self._job = None
        self._process = None

    def _running(self):
        return self._job is not None and self._job["state"] == "running"

    def _resolve(self, key):
        entry = self.pool.entries.get(key)
        if entry is None:
            raise ValueError(f"Unknown engine {key!r}.")
        script = self.scripts.get(key)
        if script is None:
            raise ValueError(f"{entry.get('name', key)} cannot be installed from the app yet: docs/SETUP.md says what it needs.")
        python = Path(self.python) if self.python else self.root / entry["runtime"]["python"]
        if not python.is_file():
            raise ValueError(f"The lab's Python is missing ({python}). Run setup.bat first.")
        return entry, self.root / script, python

    def plan(self, key):
        """What the installer would do now, from its own --dry-run: {engine, ok, paragraphs, problems}. Changes nothing."""
        _entry, script, python = self._resolve(key)
        with self._lock:
            if self._running():
                raise worker_pool.WorkerBusy(f"{self._job['name']} is being installed now.")
        try:
            done = subprocess.run([str(python), "-u", str(script), "--dry-run"], cwd=self.root, env=_environment(),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=PLAN_TIMEOUT, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"The installer did not answer within {PLAN_TIMEOUT} seconds.") from exc
        lines = (done.stdout or "").rstrip().splitlines()
        if done.returncode == 0 and lines and lines[-1].startswith(DRY_RUN_DONE):
            lines.pop()    # "Checks passed. This was a dry run": true of the command line, not of pressing Install
        steps = paragraphs("\n".join(lines))
        problems = paragraphs(re.sub(r"^error:\s*", "", (done.stderr or "").strip()))
        if done.returncode and not problems:
            problems = [f"The installer stopped without saying why (exit code {done.returncode})."]
        problems = [text if len(text) <= 800 else "…" + text[-800:] for text in problems[:8]]
        return {"engine": key, "ok": done.returncode == 0, "paragraphs": steps, "problems": problems}

    def start(self, key):
        """Start the installer with --yes and return the job (see snapshot). Raises ValueError when the engine has no
        installer, is installed already or the Python is missing, and WorkerBusy when an install or a request is running."""
        entry, script, python = self._resolve(key)
        name = entry.get("name", key)
        with self._lock:
            if self._running():
                raise worker_pool.WorkerBusy(f"{self._job['name']} is already being installed.")
        if self.pool.install_probe(entry)["installed"]:
            raise ValueError(f"{name} is already installed.")
        self.pool.hold(f"{name} is being installed")
        try:
            log_path = self._new_log(key)
            log = log_path.open("wb")
            try:
                process = subprocess.Popen([str(python), "-u", str(script), "--yes"], cwd=self.root, env=_environment(),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           creationflags=NO_WINDOW)
            except Exception:
                log.close()
                raise
        except Exception:
            self.pool.release_hold()
            raise
        job = {"engine": key, "name": name, "state": "running", "started": time.time(), "ended": None, "exit_code": None,
               "lines": deque(maxlen=KEPT_LINES), "current": "", "overwrite": False,
               "log": f"{LOG_FOLDER}/{log_path.name}"}
        with self._lock:
            self._job, self._process = job, process
        threading.Thread(target=self._pump, args=(job, process, log), name=f"install-{key}", daemon=True).start()
        return self.snapshot()

    def _new_log(self, key):
        folder = self.root / LOG_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        for old in sorted(folder.glob(f"{key}-*.log"))[:-(LOGS_KEPT - 1)]:
            try:
                old.unlink()
            except OSError:
                pass
        stem = f"{key}-{time.strftime('%Y%m%d-%H%M%S')}"
        path, count = folder / f"{stem}.log", 1
        while path.exists():
            count += 1
            path = folder / f"{stem}-{count}.log"
        return path

    def _feed(self, job, text):
        """Add output to the job as a terminal would show it: a newline ends a line, and a carriage return lets the next text
        overwrite the line, so a progress bar leaves its latest state and not every step."""
        with self._lock:
            for token in _TERMINATORS.split(text):
                if token in ("\n", "\r\n"):
                    if job["current"] or (job["lines"] and job["lines"][-1]):
                        job["lines"].append(job["current"])
                    job["current"], job["overwrite"] = "", False
                elif token == "\r":
                    job["overwrite"] = True
                elif token:
                    job["current"] = token if job["overwrite"] else job["current"] + token
                    job["overwrite"] = False

    def _commit(self, job):
        """End the line being written, as the end of the output does."""
        with self._lock:
            if job["current"]:
                job["lines"].append(job["current"])
            job["current"], job["overwrite"] = "", False

    def _pump(self, job, process, log):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        code = None
        try:
            while True:
                chunk = process.stdout.read1(4096)
                if not chunk:
                    break
                log.write(chunk)
                log.flush()
                self._feed(job, decoder.decode(chunk))
            self._feed(job, decoder.decode(b"", final=True))
            code = process.wait()
        except Exception as exc:
            self._feed(job, f"\nThe service stopped reading the installer's output: {type(exc).__name__}: {exc}\n")
            self._stop(process)
            code = process.wait()
        finally:
            try:
                for stream in (log, process.stdout):
                    try:
                        stream.close()
                    except OSError:    # a full disk can fail the last flush of the log, and the install must still end
                        pass
                self._commit(job)
                with self._lock:
                    job["exit_code"] = code if code is not None else -1
                    job["state"] = "finished" if code == 0 else "failed"
                    job["ended"] = time.time()
            finally:
                self.pool.release_hold()    # after the state is final, so a page that sees the service free sees the result

    def snapshot(self):
        """The latest install, or None when none has run since the service started: engine, name, state (running, finished
        or failed), started_at, ended_at, seconds so far, exit_code, the end of its output as lines, and its log file."""
        with self._lock:
            job = self._job
            if job is None:
                return None
            lines = list(job["lines"]) + ([job["current"]] if job["current"] else [])
            ended = job["ended"]
            return {"engine": job["engine"], "name": job["name"], "state": job["state"], "started_at": _stamp(job["started"]),
                    "ended_at": _stamp(ended) if ended else None, "seconds": round((ended or time.time()) - job["started"], 1),
                    "exit_code": job["exit_code"], "lines": lines[-TAIL_LINES:], "log": job["log"]}

    @staticmethod
    def _stop(process):
        """Kill the script and, on Windows, everything it started: pip and the speech check are its children, and a venv's
        python.exe is only a launcher for the interpreter that runs the script."""
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
            except OSError:
                pass
        process.kill()

    def close(self):
        """Stop a running install and the processes it started."""
        with self._lock:
            process = self._process if self._running() else None
        if process is None:
            return
        self._stop(process)
        try:
            process.wait(timeout=CLOSE_WAIT)
        except subprocess.TimeoutExpired:
            pass
