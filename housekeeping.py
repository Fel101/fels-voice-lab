"""Retention for the folders a running service leaves behind in out/.

Three kinds, all disposable: the temporary-audio folder of a worker that has exited (out/workers/<engine>-<pid>), the
folders of past desktop sessions (out/desktop/session-*) and the service logs of past runs (out/logs). Nothing here
touches Cast, Scenes, exports or anything else a person made.

plan() only looks. apply() removes what a plan lists, and checks every path again before it does.
"""

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

DAY = 86400
WORKER_MIN_AGE = 3600       # a younger folder may belong to a worker that is only starting
WORKER_KEEP_DAYS = 7        # a stopped worker's folder that still holds something stays this long, as evidence
DESKTOP_KEEP = 10           # the newest desktop sessions are kept whatever their age
DESKTOP_MIN_AGE_DAYS = 1
LOG_KEEP = 30               # the newest runs' service logs are kept
LOG_MIN_AGE_DAYS = 7

WORKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*-(\d+)\Z")
SESSION_NAME = re.compile(r"session-[A-Za-z0-9_]+\Z")
LOG_NAME = re.compile(r"(service-\d{8}-\d{6}-\d+)\.log(?:\.\d+)?\Z")
FOLDERS = {"worker": "workers", "desktop": "desktop", "log": "logs"}


@dataclass(frozen=True)
class Item:
    kind: str       # worker, desktop or log
    path: Path
    reason: str
    size: int
    age_days: float


def pid_may_be_alive(pid):
    """False only when the process is certainly gone; one that cannot be inspected counts as alive.
    Never os.kill(pid, 0) on Windows: there it ends the process."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: there is no such process
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def is_link(path):
    """A symbolic link or a Windows junction: something that points elsewhere and must never be followed or removed through."""
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _path: False)(path)


def tree_size(path):
    """Bytes in a file, or in every file under a folder; one that cannot be read counts as nothing."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _folders, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _entries(folder):
    try:
        return sorted(folder.iterdir(), key=lambda item: item.name)
    except OSError:
        return []


def _session_running(folder, pid_alive):
    try:
        pid = json.loads((folder / "ready.json").read_text("utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(pid, int) and pid_alive(pid)


def plan(out_dir, *, now=None, pid_alive=pid_may_be_alive, keep=()):
    """What a sweep of out_dir would remove, with the reason for each. `keep` lists paths that must stay, such as the
    log of the run that is asking."""
    out_dir = Path(out_dir)
    now = time.time() if now is None else now
    protected = {Path(path).resolve() for path in keep}
    items = []

    for entry in _entries(out_dir / "workers"):
        match = WORKER_NAME.fullmatch(entry.name)
        if not match or not entry.is_dir() or is_link(entry):
            continue
        age = now - entry.stat().st_mtime
        if age < WORKER_MIN_AGE or pid_alive(int(match.group(1))):
            continue
        if not any(entry.iterdir()):
            reason = "temporary-audio folder of a worker that has exited; it holds nothing"
        elif age >= WORKER_KEEP_DAYS * DAY:
            reason = f"temporary-audio folder of a worker that exited {age / DAY:.0f} days ago"
        else:
            continue
        items.append(Item("worker", entry, reason, tree_size(entry), age / DAY))

    sessions = [entry for entry in _entries(out_dir / "desktop")
                if SESSION_NAME.fullmatch(entry.name) and entry.is_dir() and not is_link(entry)]
    sessions.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    for entry in sessions[DESKTOP_KEEP:]:
        age = now - entry.stat().st_mtime
        if age < DESKTOP_MIN_AGE_DAYS * DAY or _session_running(entry, pid_alive):
            continue
        items.append(Item("desktop", entry, f"desktop session from {age / DAY:.0f} days ago, beyond the newest {DESKTOP_KEEP}",
                          tree_size(entry), age / DAY))

    runs = {}
    for entry in _entries(out_dir / "logs"):
        match = LOG_NAME.fullmatch(entry.name)
        if match and entry.is_file() and not is_link(entry):
            runs.setdefault(match.group(1), []).append(entry)
    for run in sorted(runs, reverse=True)[LOG_KEEP:]:
        files = runs[run]
        age = now - max(path.stat().st_mtime for path in files)
        if age < LOG_MIN_AGE_DAYS * DAY or any(path.resolve() in protected for path in files):
            continue
        for path in files:
            items.append(Item("log", path, f"service log from {age / DAY:.0f} days ago, beyond the newest {LOG_KEEP} runs",
                              path.stat().st_size, age / DAY))
    return items


def summarize(items):
    totals = {kind: {"count": 0, "bytes": 0} for kind in FOLDERS}
    for item in items:
        totals[item.kind]["count"] += 1
        totals[item.kind]["bytes"] += item.size
    return {"count": len(items), "bytes": sum(item.size for item in items), "by_kind": totals}


def apply(items, out_dir):
    """Remove what a plan lists. Each path is checked again: it must sit directly inside the folder for its kind, carry
    the name that kind uses, and not be a link. Returns what went and what could not."""
    out_dir = Path(out_dir)
    removed, freed, errors = 0, 0, []
    for item in items:
        try:
            parent = (out_dir / FOLDERS[item.kind]).resolve()
            path = Path(item.path)
            pattern = {"worker": WORKER_NAME, "desktop": SESSION_NAME, "log": LOG_NAME}[item.kind]
            if (path.parent.resolve() != parent or not pattern.fullmatch(path.name) or is_link(path)
                    or not path.exists()):
                raise ValueError("not a sweepable path")
            size = tree_size(path)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{item.path}: {exc}")
            continue
        removed += 1
        freed += size
    return {"removed": removed, "bytes": freed, "errors": errors}


def run_startup_sweep(out_dir, log=None, keep=()):
    """The sweep a starting service runs in the background. It never raises, and writes one line to the log when it
    removed or failed to remove anything."""
    try:
        result = apply(plan(out_dir, keep=keep), out_dir)
    except Exception as exc:
        result = {"removed": 0, "bytes": 0, "errors": [f"{type(exc).__name__}: {exc}"]}
    if log is not None and (result["removed"] or result["errors"]):
        log.event(f"cleanup removed {result['removed']} stale folders and files ({result['bytes']} bytes)"
                  + (f"; {len(result['errors'])} could not be removed, first: {result['errors'][0]}" if result["errors"] else ""))
    return result
