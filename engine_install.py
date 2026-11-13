"""Install an engine from the Models page: show the installer's plan, then run it and keep its progress.

An engine with a guided installer (Kokoro: tools/install_kokoro.py, the script setup-kokoro.bat runs; Phonon:
tools/install_phonon.py, the script setup-phonon.bat runs; the Chatterbox models: tools/install_chatterbox.py, the script
setup-chatterbox.bat runs; the Qwen3-TTS models: tools/install_qwen.py, the script setup-qwen.bat runs; VoxCPM2:
tools/install_voxcpm.py, the script setup-voxcpm.bat runs; Breeze TTS 2: tools/install_breeze.py, the script setup-breeze.bat
runs; Drama 3: tools/install_drama3.py, the script setup-drama3.bat runs) can be installed from its card. The page first asks
for the plan, which is the script's own --dry-run, so it shows what the script will really do and why it would refuse. Install
now then starts the script with --yes as a child process under the lab's own Python, as its launcher does (each installer makes
its model's own environment), and keeps the end of its output for the page to show. One install runs at a time. While it runs
it holds the pool's job lock, so tests, loads and clips are refused instead of competing with it for memory and disk. Closing
the service stops the install and its children; the script continues where it stopped when it is run again.

A card whose GPU/CPU switch says GPU installs the engine's GPU build for this computer's graphics card (the script's --gpu):
NVIDIA's CUDA build, or AMD's ROCm build on a computer whose card is an AMD Radeon. An engine installed with the CPU build of
PyTorch can get its GPU build afterwards, which replaces PyTorch in its environment. The models that run only on a graphics
card (Qwen3-TTS, VoxCPM2, Breeze TTS 2) always get the GPU build, whatever the switch says. gpu_build() says whether that can
work on this computer before anything is asked of the script: an NVIDIA driver, and a card and driver the build runs on, from
the installer's own card_problems(); or an AMD card whose chip AMD's ROCm build has code for, from its amd_card_problems().
The NVIDIA cards are read again every CARDS_SECONDS, so a laptop card switched on while the service runs is noticed.

The same runner installs the tools the Lab uses that are not models (TOOLS): FFmpeg, from Install FFmpeg on System & backups
and the export panel. A tool's install shares the one-at-a-time slot, so two installs never compete for the connection, but it
holds no model: tests, loads and clips go on while it runs. The latest install of each kind is kept apart (snapshot), so the
Models page never shows FFmpeg's progress as a model's.
"""

import codecs
import importlib
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import hardware
import engine_adapters
import worker_pool

# Engine id -> the script its launcher runs (setup-kokoro.bat runs tools\install_kokoro.py), or the script and the arguments
# that choose the engine's checkpoint. tests/test_engine_install.py keeps this in step with engine_checks.GUIDED_INSTALLERS and
# with the launchers themselves.
INSTALLERS = {"drama-3": "tools/install_drama3.py", **engine_adapters.table("installer")}
# Engine id -> the installer module whose --gpu puts in the GPU build of PyTorch for this computer's card: NVIDIA's, whose
# card_problems(cards) says why it would not run on this computer's NVIDIA cards, or on a computer whose card is an AMD Radeon,
# AMD's ROCm build, which its amd_card_problems(cards) judges. Qwen3-TTS, VoxCPM2 and Breeze run only on a graphics card, so
# their installers always put in the GPU build.
GPU_BUILDS = engine_adapters.table("gpu_build")
NO_GPU = ("The app installs the GPU build of {name} for an NVIDIA graphics card or an AMD Radeon, and this computer has neither "
          "an NVIDIA driver nor an AMD card that Windows lists.")
# What the installers take for their GPU build.
GPU_OPTION = "--gpu"
# Every launcher runs its script with the lab's own Python, whichever environment the engine then runs in.
LAB_PYTHON = ".venv/Scripts/python.exe"
LOG_FOLDER = "out/install"
LOGS_KEPT = 10          # per engine; the newest runs' full output stays on disk
KEPT_LINES = 200        # lines of output held in memory
TAIL_LINES = 40         # lines a snapshot hands to the page
PLAN_TIMEOUT = 120
CLOSE_WAIT = 10
# How long the NVIDIA cards' answer is kept. A laptop's NVIDIA card can be switched on or off while the service runs, and the
# Models page should notice soon after; nvidia-smi and the driver's probe take about a second together.
CARDS_SECONDS = 30
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DRY_RUN_DONE = "Checks passed."
# Tool id -> (its name, the script that installs it). See the note above.
TOOLS = {"ffmpeg": ("FFmpeg", "tools/install_ffmpeg.py")}
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

    def __init__(self, pool, *, root=None, python=None, scripts=None, gpu_builds=None, nvidia_present=None,
                 nvidia_cards=None, amd_listed=None, amd_cards=None, clock=time.monotonic, tools=None):
        self.pool = pool
        self.root = Path(root or worker_pool.LAB)
        self.python = python    # None: the lab's own Python, as the launchers use
        self.scripts = dict(INSTALLERS if scripts is None else scripts)
        self.gpu_builds = dict(GPU_BUILDS if gpu_builds is None else gpu_builds)
        self.tools = dict(TOOLS if tools is None else tools)
        self.nvidia_present = nvidia_present or hardware.nvidia_driver_present
        self.nvidia_cards = nvidia_cards or hardware.nvidia_cards
        self.amd_listed = amd_listed or hardware.amd_listed
        self.amd_cards = amd_cards or hardware.amd_cards
        self._lock = threading.Lock()
        self._cards_lock = threading.Lock()
        self._cards = None
        self._cards_at = None
        self.clock = clock
        self._amd = None
        self._jobs = {}    # the latest install of each kind: "engine" or "tool"
        self._process = None

    def _running(self):
        return any(job["state"] == "running" for job in self._jobs.values())

    def _running_name(self):
        return next(job["name"] for job in self._jobs.values() if job["state"] == "running")

    def _resolve(self, key, tool=False):
        if tool:
            if key not in self.tools:
                raise ValueError(f"Unknown tool {key!r}.")
            name, script = self.tools[key]
            entry = {"name": name}
        else:
            entry = self.pool.entries.get(key)
            if entry is None:
                raise ValueError(f"Unknown engine {key!r}.")
            script = self.scripts.get(key)
            if script is None:
                raise ValueError(
                    f"{entry.get('name', key)} cannot be installed from the app yet: docs/SETUP.md says what it needs.")
        script, *arguments = (script,) if isinstance(script, str) else script
        python = Path(self.python) if self.python else self.root / LAB_PYTHON
        if not python.is_file():
            raise ValueError(f"The lab's Python is missing ({python}). Run setup.bat first.")
        return entry, [str(python), "-u", str(self.root / script), *arguments]

    def _nvidia(self):
        """(driver present, its cards), read again when the last answer is older than CARDS_SECONDS: nvidia-smi and the
        driver's probe take a moment, and a laptop's NVIDIA card can be switched on or off while the service runs."""
        with self._cards_lock:
            now = self.clock()
            if self._cards is None or now - self._cards_at >= CARDS_SECONDS:
                present = bool(self.nvidia_present())
                self._cards, self._cards_at = (present, self.nvidia_cards() if present else []), now
            return self._cards

    def _amd_cards(self):
        """The AMD cards AMD's driver lists, read once: the driver answers in a Python of its own, which takes a moment."""
        with self._cards_lock:
            if self._amd is None:
                self._amd = list(self.amd_cards())
            return self._amd

    def with_sizes(self, status):
        """The engine status (worker_pool.engine_status) with an `install_size` on each model that is not installed and that
        Install can put in: {download_bytes, disk_bytes} for the build its Install starts (install_sizes), or None when its
        numbers cannot be read. The page's first-run note and the cards show it before anyone presses Install."""
        import install_sizes

        gpu = []    # (nvidia, AMD card) for a GPU build, worked out once and only when a row needs it

        def gpu_target():
            if not gpu:
                if self._nvidia()[0]:
                    gpu.append((True, None))
                else:
                    from tools import amd_build

                    gpu.append((False, amd_build.chosen_card(self._amd_cards(), self.root)))
            return gpu[0]

        for row in status.get("engines", []):
            if row.get("state") != "not_installed" or not (row.get("installation") or {}).get("installer"):
                continue
            nvidia, card = gpu_target() if (row.get("gpu") or {}).get("install") else (False, None)
            answer = install_sizes.model_bytes(row["id"], self.root, nvidia=nvidia, card=card)
            row["install_size"] = install_sizes.size_fields(answer)
        return status

    def gpu_build(self, key):
        """(ok, reason): whether the app can install the GPU build of an engine on this computer. It needs an installer with a
        GPU build, and either an NVIDIA driver with a card and driver that NVIDIA's build runs on, which the installer's own
        card_problems says, or an AMD card that AMD's build has code for, which its amd_card_problems says. reason is None when
        it can, and otherwise the sentence the card shows."""
        entry = self.pool.entries.get(key) or {}
        name = entry.get("name", key)
        module = self.gpu_builds.get(key)
        if module is None or key not in self.scripts:
            return False, f"The app has no GPU build of {name} to install; it runs on the CPU."
        try:
            installer = importlib.import_module(module)
            present, cards = self._nvidia()
            if present:
                problems = installer.card_problems(cards)
            elif not self.amd_listed():
                return False, NO_GPU.format(name=name)
            else:
                problems = installer.amd_card_problems(self._amd_cards(), root=self.root)
        except Exception as exc:  # noqa: BLE001 - a card that cannot be judged must not break the Models page
            return False, f"The Lab could not check {name}'s GPU build against this card ({type(exc).__name__}: {exc})."
        return (False, " ".join(problems)) if problems else (True, None)

    def plan(self, key, *, nvidia=False, tool=False):
        """What the installer would do now, from its own --dry-run: {engine, nvidia, ok, paragraphs, problems}. Changes
        nothing. nvidia asks for the plan of its GPU build (the installer's --gpu), NVIDIA's or AMD's."""
        _entry, command = self._resolve(key, tool)
        if nvidia and (tool or key not in self.gpu_builds):
            raise ValueError(f"{_entry.get('name', key)} has no GPU build the app can install.")
        with self._lock:
            if self._running():
                raise worker_pool.WorkerBusy(f"{self._running_name()} is being installed now.")
        try:
            done = subprocess.run([*command, *([GPU_OPTION] if nvidia else []), "--dry-run"], cwd=self.root,
                                  env=_environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=PLAN_TIMEOUT, creationflags=NO_WINDOW)
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
        return {"engine": key, "nvidia": nvidia, "ok": done.returncode == 0, "paragraphs": steps, "problems": problems}

    def start(self, key, *, nvidia=False, tool=False):
        """Start the installer with --yes (and --gpu for the GPU build) and return the job (see snapshot). Raises ValueError
        when the engine has no installer or no GPU build, is installed already (an engine installed with the CPU build of
        PyTorch can still get its GPU build) or the Python is missing, and WorkerBusy when an install or a request is
        running."""
        entry, command = self._resolve(key, tool)
        name = entry.get("name", key)
        if nvidia and (tool or key not in self.gpu_builds):
            raise ValueError(f"{name} has no GPU build the app can install.")
        with self._lock:
            if self._running():
                raise worker_pool.WorkerBusy(f"{self._running_name()} is already being installed.")
        if not tool and self.pool.install_probe(entry)["installed"]:
            if not nvidia:
                raise ValueError(f"{name} is already installed.")
            if self.pool.build_probe(entry)[0] != "cpu":
                raise ValueError(f"{name} is already installed, and its PyTorch is not the CPU build.")
        if not tool:
            self.pool.hold(f"{name} is being installed")
        try:
            log_path = self._new_log(key)
            log = log_path.open("wb")
            try:
                process = subprocess.Popen([*command, *([GPU_OPTION] if nvidia else []), "--yes"], cwd=self.root,
                                           env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            except Exception:
                log.close()
                raise
        except Exception:
            if not tool:
                self.pool.release_hold()
            raise
        job = {"engine": key, "kind": "tool" if tool else "engine", "name": name, "nvidia": nvidia, "state": "running",
               "started": time.time(), "ended": None,
               "exit_code": None, "lines": deque(maxlen=KEPT_LINES), "current": "", "overwrite": False,
               "log": f"{LOG_FOLDER}/{log_path.name}"}
        with self._lock:
            self._jobs[job["kind"]], self._process = job, process
        threading.Thread(target=self._pump, args=(job, process, log), name=f"install-{key}", daemon=True).start()
        return self.snapshot(job["kind"])

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
                if job["kind"] == "engine":
                    self.pool.release_hold()    # after the state is final, so a page that sees the service free sees the result

    def snapshot(self, kind="engine"):
        """The latest install of that kind ("engine", or "tool" for FFmpeg), or None when none has run since the service
        started: engine (the engine's or tool's id), name, nvidia (the GPU build),
        state (running, finished or failed), started_at, ended_at, seconds so far, exit_code, the end of its output as lines,
        and its log file."""
        with self._lock:
            job = self._jobs.get(kind)
            if job is None:
                return None
            lines = list(job["lines"]) + ([job["current"]] if job["current"] else [])
            ended = job["ended"]
            return {"engine": job["engine"], "name": job["name"], "nvidia": job["nvidia"], "state": job["state"],
                    "started_at": _stamp(job["started"]),
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
