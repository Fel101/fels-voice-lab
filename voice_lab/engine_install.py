"""Install an engine from the Models page: show the installer's plan, then run it and keep its progress.

An engine with a guided installer (Kokoro: tools/install_kokoro.py, the script tools\\setup-kokoro.bat runs; Phonon:
tools/install_phonon.py, the script tools\\setup-phonon.bat runs; the Chatterbox models: tools/install_chatterbox.py, the script
tools\\setup-chatterbox.bat runs; the Qwen3-TTS models: tools/install_qwen.py, the script tools\\setup-qwen.bat runs; VoxCPM2:
tools/install_voxcpm.py, the script tools\\setup-voxcpm.bat runs; Breeze TTS 2: tools/install_breeze.py, the script tools\\setup-breeze.bat
runs; Drama 3: tools/install_drama3.py, the script tools\\setup-drama3.bat runs) can be installed from its card. The page first asks
for the plan, which is the script's own --dry-run, so it shows what the script will really do and why it would refuse. Install
now then starts the script with --yes as a child process under the lab's own Python, as its launcher does (each installer makes
its model's own environment), and keeps the end of its output for the page to show. One install runs at a time. While it runs
it holds the pool's job lock, so tests, loads and clips are refused instead of competing with it for memory and disk. Closing
the service or pressing Stop ends the install and its children after any active package command is safe to leave; the
desktop window waits for that command too. The script continues where it stopped when it is run again.

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
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from voice_lab import hardware
from voice_lab import engine_adapters
from voice_lab import engine_uninstall
from voice_lab.install_control import CONTROL_ENV, STOPPED, InstallControl
from voice_lab import worker_pool

# Engine id -> the script its launcher runs (tools\\setup-kokoro.bat runs tools\install_kokoro.py), or the script and the arguments
# that choose the engine's checkpoint. tests/test_engine_install.py keeps this in step with engine_checks.GUIDED_INSTALLERS and
# with the launchers themselves.
INSTALLERS = engine_adapters.table("installer")
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
TOOLS = {
    "ffmpeg": ("FFmpeg", "tools/install_ffmpeg.py"),
    "asr": ("Speech recognition", "tools/install_asr.py"),
}
_STEP = re.compile(r"(?:\d+\.|-)\s")
_TERMINATORS = re.compile(r"(\r\n|\n|\r)")
ACTIVE = {"running", "stopping"}
PROTECTED_PHASES = {"environment", "packages", "finishing", "waiting"}


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
        self._tools_seen = {}
        self._resume = {}
        self._closed = False

    def _running(self):
        return next((job for job in self._jobs.values() if job["state"] in ACTIVE), None)

    def _resume_path(self, key):
        return self.root / LOG_FOLDER / f"{key}.resume.json"

    def _resume_record(self, key):
        if key not in self._resume:
            try:
                saved = json.loads(self._resume_path(key).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(saved, dict) or not isinstance(saved.get("nvidia"), bool):
                return None
            self._resume[key] = saved
        return self._resume[key]

    def _record_resume(self, key, resume):
        self._resume[key] = resume
        path = self._resume_path(key)
        try:
            if resume is None:
                path.unlink(missing_ok=True)
            else:
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(resume), encoding="utf-8")
                temporary.replace(path)
        except OSError:
            pass  # The current service can still offer a retry if the disk is full.

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
        numbers cannot be read. Also adds `install_resume` to a model whose install was stopped."""
        from voice_lab import install_sizes

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
            if row["id"] in self.scripts:
                with self._lock:
                    resume = self._resume_record(row["id"])
                if resume:
                    row["install_resume"] = resume
            if row.get("state") != "not_installed" or not (row.get("installation") or {}).get("installer"):
                continue
            nvidia, card = gpu_target() if (row.get("gpu") or {}).get("install") else (False, None)
            answer = install_sizes.model_bytes(row["id"], self.root, nvidia=nvidia, card=card)
            row["install_size"] = install_sizes.size_fields(answer)
            if row["id"] == "kokoro":
                cpu = install_sizes.model_bytes("kokoro", self.root) if nvidia or card is not None else answer
                row["install_variants"] = {
                    "cpu": install_sizes.size_fields(cpu),
                    "gpu": row["install_size"] if (row.get("gpu") or {}).get("install") else None,
                }
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
            if running := self._running():
                raise worker_pool.WorkerBusy(f"{running['name']} is being installed now.")
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
            if self._closed:
                raise worker_pool.WorkerBusy("Voice Lab is closing. Reopen it to install a model.")
            if running := self._running():
                raise worker_pool.WorkerBusy(f"{running['name']} is already being installed.")
            if not tool:
                resume = self._resume_record(key)
                if self.pool.install_probe(entry)["installed"] and not (resume and resume["nvidia"] == nvidia):
                    if not nvidia:
                        raise ValueError(f"{name} is already installed.")
                    if self.pool.build_probe(entry)[0] != "cpu":
                        raise ValueError(f"{name} is already installed, and its PyTorch is not the CPU build.")
                self.pool.hold(f"{name} is being installed", doing=f"installing {name}")
            control_dir = None
            log = None
            try:
                env = _environment()
                env.pop(CONTROL_ENV, None)
                if not tool:
                    folder = self.root / LOG_FOLDER
                    folder.mkdir(parents=True, exist_ok=True)
                    control_dir = tempfile.TemporaryDirectory(prefix="run-", dir=folder,
                                                              ignore_cleanup_errors=True)
                    env[CONTROL_ENV] = control_dir.name
                log_path = self._new_log(key)
                log = log_path.open("wb")
                process = subprocess.Popen([*command, *([GPU_OPTION] if nvidia else []), "--yes"], cwd=self.root,
                                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                           start_new_session=os.name != "nt")
            except Exception:
                try:
                    if control_dir is not None:
                        control_dir.cleanup()
                    if log is not None:
                        log.close()
                finally:
                    if not tool:
                        self.pool.release_hold()
                raise
            job = {"engine": key, "kind": "tool" if tool else "engine", "name": name, "nvidia": nvidia, "state": "running",
                   "id": uuid.uuid4().hex, "started": time.time(), "ended": None, "process": process,
                   "done": threading.Event(), "stop_phase": None,
                   "control": InstallControl(control_dir.name) if control_dir else None,
                   "exit_code": None, "lines": deque(maxlen=KEPT_LINES), "current": "", "overwrite": False,
                   "log": f"{LOG_FOLDER}/{log_path.name}"}
            self._jobs[job["kind"]] = job
            if tool:
                self._tools_seen[key] = job
            threading.Thread(target=self._pump, args=(job, log, control_dir), name=f"install-{key}", daemon=True).start()
            return self._snapshot(job)

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

    def stop(self, key, job_id):
        """Ask the matching model install to stop, without interrupting a package command."""
        with self._lock:
            job = self._jobs.get("engine")
            if not job or job["engine"] != key or job["id"] != job_id:
                raise worker_pool.WorkerBusy("That install has ended. Open Show progress for the current install.")
            if job["state"] == "running":
                self._halt(job, user_requested=True)
            return self._snapshot(job)

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

    def _pump(self, job, log, control_dir):
        process = job["process"]
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
            with self._lock:
                protected = self._halt(job)
            if protected:
                while process.stdout.read1(4096):
                    pass    # keep draining pip's output until it can exit
            code = process.wait()
        finally:
            try:
                for stream in (log, process.stdout):
                    try:
                        stream.close()
                    except OSError:    # a full disk can fail the last flush of the log, and the install must still end
                        pass
                self._commit(job)
            finally:
                with self._lock:
                    if control_dir:
                        control_dir.cleanup()
                    job["exit_code"] = code if code is not None else -1
                    phase = job["stop_phase"]
                    stopped = job["control"] is not None and (code == STOPPED or (phase and phase not in PROTECTED_PHASES))
                    job["state"] = "finished" if code == 0 else "stopped" if stopped else "failed"
                    job["ended"] = time.time()
                    if job["kind"] == "engine":
                        key = job["engine"]
                        if code == 0:
                            self._record_resume(key, None)
                        elif phase:
                            self._record_resume(key, {"nvidia": job["nvidia"], "phase": phase})
                        self.pool.release_hold()
                    job["done"].set()

    def snapshot(self, kind="engine", *, tool_key=None):
        """The latest install of that kind ("engine", or "tool" for FFmpeg), or None when none has run since the service
        started: engine (the engine's or tool's id), name, nvidia (the GPU build),
        state, started_at, ended_at, seconds so far, exit_code, the end of its output as lines, and its log file.
        Model jobs report an id and phase for Stop."""
        with self._lock:
            job = self._jobs.get(kind) if tool_key is None else self._tools_seen.get(tool_key)
            return self._snapshot(job) if job else None

    def _snapshot(self, job):
        lines = list(job["lines"]) + ([job["current"]] if job["current"] else [])
        ended = job["ended"]
        answer = {"engine": job["engine"], "name": job["name"], "nvidia": job["nvidia"], "state": job["state"],
                  "started_at": _stamp(job["started"]),
                  "ended_at": _stamp(ended) if ended else None, "seconds": round((ended or time.time()) - job["started"], 1),
                  "exit_code": job["exit_code"], "lines": lines[-TAIL_LINES:], "log": job["log"]}
        if job["control"] is not None:
            phase = job["stop_phase"]
            if job["state"] == "running":
                phase = job["control"].current_phase()
            answer.update(id=job["id"], phase=phase)
        return answer

    def uninstall_plan(self, key):
        """What uninstalling a model deletes and what stays (engine_uninstall.plan), for the confirmation."""
        return engine_uninstall.plan(self.pool.entries, key, self.root, self.pool.install_probe)

    def uninstall(self, key):
        """Uninstall a model (engine_uninstall.run). The model worker is held, and every model unloaded, while its files are
        deleted, so no request can start one of them halfway. Raises WorkerBusy while an install or a request runs."""
        name = self.pool.entries.get(key, {}).get("name", key)
        with self._lock:
            if running := self._running():
                raise worker_pool.WorkerBusy(f"{running['name']} is being installed. Uninstall {name} when it has finished.")
        self.pool.hold(f"{name} is being uninstalled", doing=f"uninstalling {name}")
        try:
            self.pool.unload_held()
            result = engine_uninstall.run(self.pool.entries, key, self.root, self.pool.install_probe)
            if not result["failed"]:
                with self._lock:
                    self._record_resume(key, None)
            return result
        finally:
            self.pool.release_hold()

    @staticmethod
    def _stop(process):
        """Kill the script and, on Windows, everything it started: pip and the speech check are its children, and a venv's
        python.exe is only a launcher for the interpreter that runs the script."""
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               creationflags=NO_WINDOW, timeout=CLOSE_WAIT)
            except (OSError, subprocess.TimeoutExpired):
                pass
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _halt(self, job, *, user_requested=False):
        """Signal a stop, then terminate the process tree if the current phase can be interrupted."""
        if job["process"].poll() is not None:
            return False
        control = job["control"]
        try:
            phase = control.request_stop() if control else None
        except OSError:
            if user_requested:
                raise
            return True    # Without the stop flag, a safe phase could change to pip before a kill.
        protected = phase in PROTECTED_PHASES
        if user_requested or protected:
            job["stop_phase"] = phase
            job["state"] = "stopping"
        if not protected:
            self._stop(job["process"])
        return protected

    def close(self, waiting=None):
        """Stop a running install and the processes it started. While a package command finishes, wait for it, and first call
        `waiting(name)`: the service uses it to tell the desktop window to wait as well (desktop_lifecycle.note_finishing),
        which would otherwise end the service and pip with it after 30 seconds."""
        with self._lock:
            self._closed = True
            job = self._running()
            if job is None:
                return
            protected = self._halt(job) if job["state"] == "running" else job["stop_phase"] in PROTECTED_PHASES
        if protected and waiting is not None:
            waiting(job["name"])
        job["done"].wait(timeout=None if protected else CLOSE_WAIT)
