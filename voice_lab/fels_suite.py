"""What the apps of Fel's Studio agree on: the suite contract, number 1.

Fel's Voice Slate, Voice Lab, NPC Forge and Altera each carry this same file, with its test cases (suite_vectors.json).
Change it only by changing the contract, never in one app alone. Standard library only, Python 3.8 or later, and nothing
from the app around it.

- suite_dir(): the suite folder, per user, where the address files, the inboxes and the GPU lock live.
- write_address(), read_address(), remove_address(): how a running app tells the others where it listens.
- health_block(), identify(), find_app(): the `fels` block in /api/health, and finding an app by asking it.
- inbox_dir(), inbox_write(), inbox_list(), inbox_done(): handing a file to another app without reading its folders.
- GpuLock, gpu_status(), holder_sentence(): sharing the graphics card, shared or alone (NPC Forge's One model at a time).
- unload_outside(): unloading the models LM Studio and Ollama hold on this PC.
- check_vectors(): runs suite_vectors.json, so every app tests this file the same way.
"""

import datetime
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath

FELS_SUITE_VERSION = "1.0"
CONTRACT = 1
SUITE_NAME = "Fel's Studio"

# The four apps: their ids, names, where each listens first, where an older release listened, and the environment variable
# a person can set to point at one.
APPS = {
    "voice-slate": {"name": "Fel's Voice Slate", "short": "Voice Slate", "port": None, "old_ports": (), "url_env": None},
    "voice-lab": {"name": "Fel's Voice Lab", "short": "Voice Lab", "port": 8765, "old_ports": (), "url_env": "VOICE_LAB_URL"},
    "npc-forge": {
        "name": "Fel's NPC Forge",
        "short": "NPC Forge",
        "port": 8766,
        "old_ports": (8765,),
        "url_env": "NPCFORGE_URL",
    },
    "altera": {"name": "Fel's Altera", "short": "Altera", "port": 8767, "old_ports": (5001,), "url_env": "ALTERA_URL"},
}
# Every port the suite uses or meets (contract 2). An example in the docs that needs "some other port" uses 8775.
PORTS = {
    "voice-lab": 8765,
    "npc-forge": 8766,
    "altera": 8767,
    "npc-forge-second-copy": 8768,
    "voice-lab-mcp": 8769,
    "altera-voice": 8770,
    "comfyui": 8188,
    "lm-studio": 1234,
    "ollama": 11434,
}
# What each app's inbox takes in this round (contract 6).
INBOX_KINDS = {"voice-lab": (".voiceslate",), "altera": (".charx", ".png"), "npc-forge": (), "voice-slate": ()}
INBOX_MAX_LISTED = 200

_TIME = "%Y-%m-%dT%H:%M:%SZ"
_FILE_TIME = "%Y%m%dT%H%M%SZ"
_UTC = datetime.timezone.utc


def short_name(app):
    """How an app is named in a sentence: "Voice Lab", or the id itself for an app this file does not know."""
    return APPS[app]["short"] if app in APPS else str(app)


def _now():
    return datetime.datetime.now(_UTC).replace(microsecond=0)


def _iso(moment):
    return moment.astimezone(_UTC).strftime(_TIME)


def _moment(text):
    """A "2026-12-01T10:02:11Z" time as an aware datetime, or None."""
    try:
        return datetime.datetime.strptime(text, _TIME).replace(tzinfo=_UTC)
    except (TypeError, ValueError):
        return None


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ---- the suite folder (contract 3) -------------------------------------------------------------------------------------


def default_suite_dir(platform=None, environ=None, home=None):
    """The suite folder when FELS_SUITE_DIR is not set, as a pure path for `platform` (in sys.platform's spelling):
    %LOCALAPPDATA%\\Fels on Windows, ~/Library/Application Support/Fels on macOS, else $XDG_STATE_HOME/fels or
    ~/.local/state/fels."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform.startswith("win"):
        home = PureWindowsPath(Path.home() if home is None else home)
        return PureWindowsPath(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Fels"
    home = PurePosixPath(Path.home() if home is None else home)
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Fels"
    state = environ.get("XDG_STATE_HOME")
    if state and PurePosixPath(state).is_absolute():  # the XDG rule: a relative path is ignored
        return PurePosixPath(state) / "fels"
    return home / ".local" / "state" / "fels"


def _resolve_suite_dir(environ):
    environ = os.environ if environ is None else environ
    default = Path(default_suite_dir(environ=environ))
    given = (environ.get("FELS_SUITE_DIR") or "").strip()
    if not given:
        return default, None
    path = Path(given)
    if not path.is_absolute():
        return default, f"FELS_SUITE_DIR is {given!r}, which is not a full path, so the suite folder is {default}."
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        return default, f"FELS_SUITE_DIR names {path}, which cannot be made ({reason}), so the suite folder is {default}."
    return path, None


def suite_dir(environ=None):
    """The suite folder (contract 3): FELS_SUITE_DIR when it names a full path that can be made, else this system's default.
    Reading never makes it; the first app that writes an address file, an inbox or the GPU lock does."""
    return _resolve_suite_dir(environ)[0]


def suite_dir_problem(environ=None):
    """The sentence to log, and show once, when FELS_SUITE_DIR could not be used; None when all is well."""
    return _resolve_suite_dir(environ)[1]


# ---- small files, written whole ----------------------------------------------------------------------------------------


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path, value):
    """Write `value` to `path` whole: a temporary file in the same folder, then a replace, so a reader never sees half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.stem + "-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:  # Windows: a reader has the file open this instant
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _unlink(path):
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def process_alive(pid):
    """Whether a process with this id is running now."""
    if not _is_int(pid) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, as another user's process
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---- address files (contract 4) ----------------------------------------------------------------------------------------


def address_path(app, environ=None):
    return suite_dir(environ) / "apps" / (app + ".json")


def _valid_address(entry, app):
    return (
        isinstance(entry, dict)
        and _is_int(entry.get("contract"))
        and entry["contract"] >= 1
        and entry.get("app") == app
        and isinstance(entry.get("url"), str)
        and entry["url"].startswith(("http://", "https://"))
        and _is_int(entry.get("pid"))
    )


def read_address(app, environ=None, *, alive=None):
    """Where a running copy of `app` listens, from its address file: the file's fields as a dict, or None when there is no
    file, it cannot be read, it is not that app's, or the process that wrote it has gone (a stale file)."""
    alive = process_alive if alive is None else alive
    entry = _read_json(address_path(app, environ))
    if not _valid_address(entry, app):
        return None
    return entry if alive(entry["pid"]) else None


def write_address(app, url, version, api, instance, environ=None, *, pid=None, alive=None, now=None):
    """Announce this copy of `app` once it is listening. True when the file now names this copy; False when another running
    copy is named there, which stays: this copy works but is not announced."""
    pid = os.getpid() if pid is None else pid
    current = read_address(app, environ, alive=alive)
    if current is not None and current["pid"] != pid:
        return False
    entry = {
        "contract": CONTRACT,
        "app": app,
        "name": APPS[app]["name"] if app in APPS else app,
        "version": version,
        "api": api,
        "url": url,
        "pid": pid,
        "instance": instance,
        "started": _iso(now or _now()),
    }
    _write_json(address_path(app, environ), entry)
    return True


def remove_address(app, environ=None, *, pid=None):
    """Take this copy's address file away when it stops. Another copy's file stays. True when it was removed."""
    pid = os.getpid() if pid is None else pid
    path = address_path(app, environ)
    entry = _read_json(path)
    if not isinstance(entry, dict) or entry.get("pid") != pid:
        return False
    return _unlink(path)


# ---- health, and finding an app (contract 5) ---------------------------------------------------------------------------


def health_block(app, version, api):
    """The `fels` block an app adds to its GET /api/health answer."""
    return {"contract": CONTRACT, "app": app, "version": version, "api": api}


def identify(answer):
    """Which suite app gave this /api/health answer: its app id, or None for anything else.

    An app of this contract says so in its `fels` block. Before it (this round only): Voice Lab 0.4 names itself in `app`, and
    0.3 answers with its version, status and workers; NPC Forge 1.0 answers with ComfyUI's state (`online`) and its GPU rule
    (`gpu_rule`), which no Voice Lab has, so a Lab is never taken for Forge."""
    if not isinstance(answer, dict):
        return None
    block = answer.get("fels")
    if isinstance(block, dict):
        app = block.get("app")
        return app if isinstance(app, str) and app else None
    if answer.get("app") == APPS["voice-lab"]["name"] or {"version", "status", "workers"} <= set(answer):
        return "voice-lab"
    if {"online", "gpu_rule"} <= set(answer):
        return "npc-forge"
    return None


def ask_health(url, timeout=2.0):
    """What answers GET /api/health at `url`, as a dict, or None when nothing does or the answer is not a JSON object."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this computer: no proxy
    try:
        with opener.open(url.rstrip("/") + "/api/health", timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return answer if isinstance(answer, dict) else None


def find_app(app, ask=None, *, given=None, environ=None, alive=None):
    """Where `app` answers, tried in contract 5's order: the address the person set (`given`, then the app's environment
    variable), its address file, its port, then where an older release listened. An address counts only when its health
    answer is that app's (identify), so another app on the port is never taken for it.

    `ask(url)` returns a health answer or None (ask_health by default; a test passes its own). The result is
    {"url", "source", "health", "notes"}: "url" is None when the app was not found; "source" is "given", "environment",
    "address-file", "port" or "old-port"; "notes" are sentences to show, such as "That address is Voice Lab, not NPC Forge."
    """
    ask = ask_health if ask is None else ask
    environ = os.environ if environ is None else environ
    info = APPS.get(app, {})
    name = short_name(app)
    candidates = []
    if given:
        candidates.append((given, "given"))
    variable = info.get("url_env")
    if variable and environ.get(variable):
        candidates.append((environ[variable], "environment"))
    entry = read_address(app, environ, alive=alive)
    if entry:
        candidates.append((entry["url"], "address-file"))
    if info.get("port"):
        candidates.append((f"http://127.0.0.1:{info['port']}", "port"))
    for port in info.get("old_ports", ()):
        candidates.append((f"http://127.0.0.1:{port}", "old-port"))
    notes, tried = [], set()
    for url, source in candidates:
        url = url.rstrip("/")
        if url in tried:
            continue
        tried.add(url)
        answer = ask(url)
        found = identify(answer)
        if found == app:
            return {"url": url, "source": source, "health": answer, "notes": notes}
        if source in ("given", "environment"):
            if answer is None:
                notes.append(f"Nothing answers at {url}, the address set for {name}.")
            else:
                other = short_name(found) if found else "another program"
                notes.append(f"That address is {other}, not {name}.")
    notes.append(f"{name} is not running.")
    return {"url": None, "source": None, "health": None, "notes": notes}


# ---- the inbox (contract 6) --------------------------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^\w]+")
_INBOX_NAME = re.compile(r"^(\d{8}T\d{6}Z)-(.+)$")


def inbox_dir(app, environ=None, *, make=False):
    """`app`'s inbox folder. The receiver makes it at its first start (make=True); a sender offers "Send to ..." only when
    it exists."""
    path = suite_dir(environ) / "inbox" / app
    if make:
        path.mkdir(parents=True, exist_ok=True)
    return path


def inbox_name(sender, name, now=None):
    """The file name a handoff gets: "<UTC time>-<sender id>-<short name>.<ext>", from a name such as "Maya, take 3.voiceslate".
    The short name keeps letters, digits and underscores, joins the rest with "-", and is at most 40 characters."""
    stem, ext = os.path.splitext(name)
    stem = _UNSAFE.sub("-", stem).strip("-_")[:40].strip("-_") or "file"
    return f"{(now or _now()).astimezone(_UTC).strftime(_FILE_TIME)}-{sender}-{stem}{ext.lower()}"


def parse_inbox_name(name):
    """(when it was sent, as "2026-12-01T10:02:11Z", or None; the sender's app id, or None) from a handoff's file name."""
    match = _INBOX_NAME.match(name)
    if not match:
        return None, None
    moment = None
    try:
        moment = _iso(datetime.datetime.strptime(match.group(1), _FILE_TIME).replace(tzinfo=_UTC))
    except ValueError:
        pass
    rest = match.group(2)
    sender = next((app for app in sorted(APPS, key=len, reverse=True) if rest.startswith(app + "-")), None)
    return moment, sender


def inbox_write(receiver, sender, name, data, environ=None, *, now=None):
    """Hand `data` (bytes) to `receiver`: written under a temporary name that starts with ".", then renamed, so the receiver
    never sees half a file. `name` is a short name with its extension. Returns the new file's path.

    FileNotFoundError: the receiver has no inbox (it is not installed, or has never started): offer "Save as..." instead.
    ValueError: the receiver does not take this kind of file."""
    ext = os.path.splitext(name)[1].lower()
    if ext not in INBOX_KINDS.get(receiver, ()):
        raise ValueError(f"{short_name(receiver)} does not take {ext or 'extensionless'} files.")
    folder = inbox_dir(receiver, environ)
    if not folder.is_dir():
        raise FileNotFoundError(f"{short_name(receiver)} has no inbox yet: it is not installed, or has not been started.")
    final = folder / inbox_name(sender, name, now)
    handle, temporary = tempfile.mkstemp(prefix="." + final.stem[:60] + "-", suffix=".part", dir=str(folder))
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        stem, number = final.stem, 2
        while True:
            if not final.exists():
                try:
                    os.rename(temporary, final)  # never replaces: on Windows a name taken this instant raises
                    return final
                except FileExistsError:
                    pass
            final = folder / f"{stem}-{number}{ext}"
            number += 1
    finally:
        if os.path.exists(temporary):
            _unlink(temporary)


def _is_link(entry):
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT: a junction or another kind of link


def inbox_list(app, kinds, max_bytes, environ=None):
    """What waits in `app`'s inbox, oldest first, at most 200: [{"path", "name", "sender", "sender_name", "sent", "size",
    "code", "problem"}]. "code" is None for a file the app can take, "kind" for another kind of file and "size" for one
    over `max_bytes`; "problem" says why in a sentence. Files being written (".") and links are left out."""
    folder = inbox_dir(app, environ)
    kinds = tuple(kind.lower() for kind in kinds)
    found = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.name.startswith(".") or _is_link(entry) or not entry.is_file(follow_symlinks=False):
                continue
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        sent, sender = parse_inbox_name(entry.name)
        ext = os.path.splitext(entry.name)[1].lower()
        code = problem = None
        if ext not in kinds:
            code, problem = "kind", f"{short_name(app)} does not take {ext or 'extensionless'} files."
        elif size > max_bytes:
            code = "size"
            problem = f"It is {size / 1048576:.1f} MB; {short_name(app)} takes files up to {max_bytes / 1048576:.0f} MB."
        found.append(
            {
                "path": entry.path,
                "name": entry.name,
                "sender": sender,
                "sender_name": short_name(sender) if sender else None,
                "sent": sent,
                "size": size,
                "code": code,
                "problem": problem,
            }
        )
    found.sort(key=lambda item: item["name"])
    return found[:INBOX_MAX_LISTED]


def _recycle(path):
    """Send a file to the Recycle Bin on Windows; delete it elsewhere."""
    if os.name != "nt":
        os.remove(path)
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = os.path.abspath(path) + "\0"  # the list ends with a second NUL, which ctypes adds
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted or os.path.exists(path):
        raise OSError(f"Windows could not move {os.path.basename(path)} to the Recycle Bin (code {result}).")


def inbox_done(path, environ=None, *, discard=None):
    """After an import or a Dismiss: the file goes to the Recycle Bin on Windows, and is deleted elsewhere. Only a file that
    lies directly in one of the suite's inboxes is touched; anything else is a ValueError."""
    path = Path(path)
    inboxes = os.path.normcase(os.path.abspath(suite_dir(environ) / "inbox"))
    if os.path.normcase(os.path.abspath(path.parent.parent)) != inboxes or path.name.startswith("."):
        raise ValueError("Only a file waiting in an inbox of Fel's Studio can be cleared this way.")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} is not a file waiting in the inbox.")
    (discard or _recycle)(str(path))


# ---- the GPU lock (contract 8) -----------------------------------------------------------------------------------------


class _OsLock:
    """One open handle on gpu/lock, locked on its first byte: shared, or alone (exclusive). Never waits by itself."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a+b")
        self._locked = False

    def try_lock(self, alone):
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            overlapped = _overlapped()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.LockFileEx.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
            )
            kernel32.LockFileEx.restype = wintypes.BOOL
            flags = 0x1 | (0x2 if alone else 0)  # LOCKFILE_FAIL_IMMEDIATELY, LOCKFILE_EXCLUSIVE_LOCK
            handle = msvcrt.get_osfhandle(self._file.fileno())
            self._locked = bool(kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)))
            return self._locked
        import fcntl

        try:
            fcntl.flock(self._file.fileno(), (fcntl.LOCK_EX if alone else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError:
            return False
        self._locked = True
        return True

    def close(self):
        """Unlock, and close the handle."""
        if self._locked and os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
            kernel32.UnlockFileEx.restype = wintypes.BOOL
            kernel32.UnlockFileEx(msvcrt.get_osfhandle(self._file.fileno()), 0, 1, 0, ctypes.byref(_overlapped()))
        elif self._locked:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._locked = False
        self._file.close()


def _overlapped():
    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    return OVERLAPPED()


def _gpu_dir(environ):
    return suite_dir(environ) / "gpu"


def _valid_entry(entry):
    return isinstance(entry, dict) and isinstance(entry.get("app"), str) and _is_int(entry.get("pid"))


def gpu_holders(environ=None, *, alive=None):
    """Who holds the GPU lock now, oldest first: [{"contract", "app", "pid", "what", "since", "alone"}]. For showing, never
    for deciding. A holder file whose process has gone is stale: it is left out and removed."""
    alive = process_alive if alive is None else alive
    folder = _gpu_dir(environ) / "holders"
    holders = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    for name in names:
        if name.startswith(".") or not name.endswith(".json"):
            continue
        entry = _read_json(folder / name)
        if not _valid_entry(entry):
            continue
        if alive(entry["pid"]):
            holders.append(entry)
        else:
            _unlink(folder / name)
    holders.sort(key=lambda entry: (str(entry.get("since")), entry["app"]))
    return holders


def alone_wanted(environ=None, *, alive=None, mine=None):
    """The app that wants, or has, the GPU to itself (gpu/alone.json), when its process is running; else None. `mine` is an
    (app, pid) pair to leave out: a lock never waits for itself."""
    alive = process_alive if alive is None else alive
    entry = _read_json(_gpu_dir(environ) / "alone.json")
    if not _valid_entry(entry) or not alive(entry["pid"]):
        return None
    if mine is not None and (entry["app"], entry["pid"]) == tuple(mine):
        return None
    return entry


def gpu_status(environ=None, *, alive=None):
    """For the GPU light: {"holders": gpu_holders(), "alone": alone_wanted()}."""
    return {"holders": gpu_holders(environ, alive=alive), "alone": alone_wanted(environ, alive=alive)}


def holder_sentence(entry):
    """One sentence about a holder (or alone.json's entry), for a wait or a refusal: "NPC Forge is using the whole GPU for
    pictures (One model at a time) since 10:02." The time is this computer's."""
    name = short_name(entry.get("app"))
    what = entry.get("what") or "a model"
    moment = _moment(entry.get("since"))
    since = f" since {moment.astimezone().strftime('%H:%M')}" if moment else ""
    if entry.get("alone", True):  # a holder file says; alone.json's entry is always alone
        return f"{name} is using the whole GPU for {what} (One model at a time){since}."
    return f"{name} is using the GPU for {what}{since}."


class GpuLock:
    """This app's hold on the graphics card (contract 8), for as long as a model stays loaded on it. Work on the CPU takes
    nothing. One GpuLock for each app in a process.

    Shared, by default: several apps hold it together, as a small voice model and a chat model share a card. Alone: only
    NPC Forge, and only while its One model at a time switch is on. Wanting it alone writes gpu/alone.json first; from
    then on a shared take waits, so nothing loads again while Forge clears the card. Forge then asks the others to unload,
    and its alone take waits until every shared holder has let go.

    `alive`, `clock` and `sleep` are for tests."""

    def __init__(self, app, environ=None, *, pid=None, alive=None, clock=None, sleep=None):
        self.app = app
        self.environ = environ
        self.pid = os.getpid() if pid is None else pid
        self._alive = process_alive if alive is None else alive
        self._clock = time.monotonic if clock is None else clock
        self._sleep = time.sleep if sleep is None else sleep
        self._handle = None
        self._wanted = False
        self.alone = False
        self.what = None
        self.since = None

    @property
    def held(self):
        return self._handle is not None

    def _holder_path(self):
        return _gpu_dir(self.environ) / "holders" / f"{self.app}-{self.pid}.json"

    def _alone_path(self):
        return _gpu_dir(self.environ) / "alone.json"

    def alone_wanted(self):
        """Another app that wants or has the card to itself, or None."""
        return alone_wanted(self.environ, alive=self._alive, mine=(self.app, self.pid))

    def holders(self):
        return gpu_holders(self.environ, alive=self._alive)

    def want_alone(self, what):
        """Say that this app is about to take the card alone (contract 8, step 1). From now on shared takes wait. False, and
        nothing written, when another running app wants the card alone already."""
        if self.alone_wanted() is not None:
            return False
        _write_json(
            self._alone_path(), {"contract": CONTRACT, "app": self.app, "pid": self.pid, "what": what, "since": _iso(_now())}
        )
        self._wanted = True
        return True

    def drop_alone(self):
        """No longer wanting the card alone: alone.json goes, if it is this lock's."""
        if not self._wanted:
            return
        self._wanted = False
        entry = _read_json(self._alone_path())
        if _valid_entry(entry) and (entry["app"], entry["pid"]) == (self.app, self.pid):
            _unlink(self._alone_path())

    def acquire(self, what, alone=False, timeout=None, cancel=None, poll=0.2):
        """Take the lock, shared or alone, waiting at most `timeout` seconds (0 tries once; None waits until `cancel()` is
        true). True when it is held. Taking it again the same way only says what is loaded now."""
        if self.held:
            if alone != self.alone:
                raise ValueError("This app holds the GPU lock the other way; release it first.")
            if what != self.what:
                self.what, self.since = what, _iso(_now())
                self._write_holder()
            return True
        deadline = None if timeout is None else self._clock() + timeout
        handle = _OsLock(_gpu_dir(self.environ) / "lock")
        try:
            while True:
                if alone and not self._wanted:
                    self.want_alone(what)
                ready = self._wanted if alone else self.alone_wanted() is None
                if ready and handle.try_lock(alone):
                    break
                if (cancel is not None and cancel()) or (deadline is not None and self._clock() >= deadline):
                    handle.close()
                    if alone:
                        self.drop_alone()
                    return False
                self._sleep(poll)
        except BaseException:
            handle.close()
            if alone:
                self.drop_alone()
            raise
        self._handle, self.alone, self.what, self.since = handle, alone, what, _iso(_now())
        self._write_holder()
        return True

    def release(self):
        """Let go: the holder file goes just before the lock, and alone.json with it."""
        if self._handle is not None:
            _unlink(self._holder_path())
            self._handle.close()
            self._handle = None
        if self._wanted:
            self.drop_alone()
        self.alone, self.what, self.since = False, None, None

    def _write_holder(self):
        entry = {
            "contract": CONTRACT,
            "app": self.app,
            "pid": self.pid,
            "what": self.what,
            "since": self.since,
            "alone": self.alone,
        }
        _write_json(self._holder_path(), entry)


# ---- models outside the lock -------------------------------------------------------------------------------------------


def _ask_json(method, url, body=None, timeout=3.0):
    """A JSON request to a program on this computer: its answer as a dict, or None when nothing answers."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except (OSError, ValueError, http.client.HTTPException):
        return None
    try:
        answer = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def _find_lms():
    """LM Studio's `lms` command: on PATH, or where LM Studio installs it."""
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    return str(candidate) if candidate.is_file() else None


def unload_outside(*, ask_json=None, run=None, find_lms=None, timeout=20.0):
    """Unload the models LM Studio and Ollama hold on this PC (contract 8, NPC Forge clearing the card).

    Only a program whose server answers on this computer at its usual port is touched: LM Studio (1234) through its own
    `lms unload --all`, which is never used to start it, and Ollama (11434) with keep_alive 0 for each model it lists. They
    load a model again when next asked. Returns one entry for each program that held a model: {"program", "models", "done",
    "problem"}; "problem" is a sentence when a model could not be unloaded."""
    ask_json = _ask_json if ask_json is None else ask_json
    run = subprocess.run if run is None else run
    find_lms = _find_lms if find_lms is None else find_lms
    results = []

    listing = ask_json("GET", f"http://127.0.0.1:{PORTS['lm-studio']}/api/v0/models") or {}
    data = listing.get("data")
    models = (
        [item.get("id") for item in data if isinstance(item, dict) and item.get("state") == "loaded"]
        if isinstance(data, list)
        else []
    )
    if models:
        entry = {"program": "LM Studio", "models": models, "done": False, "problem": None}
        lms = find_lms()
        if not lms:
            entry["problem"] = "LM Studio's lms command was not found, so its model stays loaded: unload it in LM Studio."
        else:
            try:
                done = run([lms, "unload", "--all"], capture_output=True, text=True, timeout=timeout)
                entry["done"] = done.returncode == 0
                if not entry["done"]:
                    entry["problem"] = "LM Studio did not unload its model: unload it in LM Studio."
            except (OSError, subprocess.SubprocessError):
                entry["problem"] = "LM Studio's lms command did not run, so its model stays loaded: unload it in LM Studio."
        results.append(entry)

    running = ask_json("GET", f"http://127.0.0.1:{PORTS['ollama']}/api/ps") or {}
    items = running.get("models")
    models = (
        [item.get("model") or item.get("name") for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    )
    models = [model for model in models if isinstance(model, str) and model]
    if models:
        failed = [
            model
            for model in models
            if ask_json("POST", f"http://127.0.0.1:{PORTS['ollama']}/api/generate", {"model": model, "keep_alive": 0}) is None
        ]
        entry = {"program": "Ollama", "models": models, "done": not failed, "problem": None}
        if failed:
            entry["problem"] = f"Ollama did not unload {', '.join(failed)}: stop it with `ollama stop`."
        results.append(entry)
    return results


# ---- the shared test cases ---------------------------------------------------------------------------------------------


def check_vectors(path):
    """Run suite_vectors.json against this file. Returns a list of failures, each a sentence; empty when all pass. Every app
    runs it in its tests, so the shared file behaves the same in every repository."""
    vectors = _read_json(path)
    if vectors is None:
        return [f"{path} cannot be read."]
    failures = []
    if vectors.get("suite_version") != FELS_SUITE_VERSION or vectors.get("contract") != CONTRACT:
        failures.append("The vectors are for another version of fels_suite.py.")
    answers = vectors.get("answers", {})

    def answer(value):
        return answers.get(value[1:]) if isinstance(value, str) and value.startswith("$") else value

    def fail(group, case, got, expected):
        failures.append(f"{group}: {case.get('what', case)}: got {got!r}, expected {expected!r}")

    for case in vectors.get("default_suite_dir", []):
        got = str(default_suite_dir(case["platform"], case["environ"], case["home"]))
        if got != case["expect"]:
            fail("default_suite_dir", case, got, case["expect"])
    for case in vectors.get("health_block", []):
        got = health_block(case["app"], case["version"], case["api"])
        if got != case["expect"]:
            fail("health_block", case, got, case["expect"])
    for case in vectors.get("identify", []):
        got = identify(answer(case["answer"]))
        if got != case["expect"]:
            fail("identify", case, got, case["expect"])
    for case in vectors.get("inbox_names", []):
        got = inbox_name(case["sender"], case["name"], _moment(case["now"]))
        if got != case["expect"]:
            fail("inbox_names", case, got, case["expect"])
    for case in vectors.get("inbox_parse", []):
        got = list(parse_inbox_name(case["name"]))
        if got != case["expect"]:
            fail("inbox_parse", case, got, case["expect"])

    folder = tempfile.mkdtemp(prefix="fels-suite-vectors-")
    try:
        for number, case in enumerate(vectors.get("addresses", [])):
            environ = {"FELS_SUITE_DIR": os.path.join(folder, f"a{number}")}
            living = set(case["alive"])
            if case["file"] is not None:
                _write_json(address_path(case["app"], environ), case["file"])
            got = read_address(case["app"], environ, alive=living.__contains__) is not None
            if got != case["expect"]:
                fail("addresses", case, got, case["expect"])
        for number, case in enumerate(vectors.get("find", [])):
            environ = dict(case.get("environ", {}), FELS_SUITE_DIR=os.path.join(folder, f"f{number}"))
            if case.get("address") is not None:
                _write_json(address_path(case["app"], environ), case["address"])
            replies = {url: answer(value) for url, value in case["answers"].items()}
            living = set(case.get("alive", []))
            found = find_app(case["app"], replies.get, given=case.get("given"), environ=environ, alive=living.__contains__)
            got = {"url": found["url"], "source": found["source"], "notes": found["notes"]}
            if got != case["expect"]:
                fail("find", case, got, case["expect"])
        for number, case in enumerate(vectors.get("inbox_list", [])):
            environ = {"FELS_SUITE_DIR": os.path.join(folder, f"i{number}")}
            inbox = inbox_dir(case["app"], environ, make=True)
            for name, size in case["files"]:
                with open(inbox / name, "wb") as output:
                    output.write(b"\0" * size)
            listed = inbox_list(case["app"], case["kinds"], case["max_bytes"], environ)
            got = [[item["name"], item["sender"], item["code"]] for item in listed]
            if got != case["expect"]:
                fail("inbox_list", case, got, case["expect"])
        for number, case in enumerate(vectors.get("holders", [])):
            environ = {"FELS_SUITE_DIR": os.path.join(folder, f"h{number}")}
            for name, entry in case["files"].items():
                _write_json(_gpu_dir(environ) / "holders" / name, entry)
            if case.get("alone") is not None:
                _write_json(_gpu_dir(environ) / "alone.json", case["alone"])
            living = set(case["alive"])
            status = gpu_status(environ, alive=living.__contains__)
            got = [[entry["app"] for entry in status["holders"]], status["alone"]["app"] if status["alone"] else None]
            expected = [case["expect_holders"], case["expect_alone"]]
            if got != expected:
                fail("holders", case, got, expected)
        takers = vectors.get("takers", {})
        for number, case in enumerate(vectors.get("locks", [])):
            failures.extend(_check_lock_case(case, takers, os.path.join(folder, f"l{number}")))
        for case in vectors.get("unload_outside", []):
            failures.extend(_check_unload_case(case))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return failures


def _check_lock_case(case, takers, folder):
    environ = {"FELS_SUITE_DIR": folder}
    living = {taker["pid"] for taker in takers.values()}
    locks = {
        name: GpuLock(taker["app"], environ, pid=taker["pid"], alive=living.__contains__, sleep=lambda seconds: None)
        for name, taker in takers.items()
    }
    failures = []
    try:
        for step in case["steps"]:
            who, action = step[0], step[1]
            lock = locks[who]
            if action == "take":
                got = lock.acquire(f"a model of {who}", alone=step[2] == "alone", timeout=0)
                if got != step[3]:
                    failures.append(f"locks: {case['what']}: {who} taking it {step[2]} gave {got}, expected {step[3]}")
            elif action == "release":
                lock.release()
            elif action == "want_alone":
                lock.want_alone("pictures")
            elif action == "drop_alone":
                lock.drop_alone()
            elif action == "dies":
                living.discard(lock.pid)
            elif action == "holders":
                got = sorted(entry["app"] for entry in gpu_holders(environ, alive=living.__contains__))
                if got != sorted(step[2]):
                    failures.append(f"locks: {case['what']}: holders {got}, expected {sorted(step[2])}")
    finally:
        for lock in locks.values():
            lock.release()
    return failures


def _check_unload_case(case):
    calls = []

    def ask_json(method, url, body=None):
        calls.append([method, url, body])
        return case["servers"].get(f"{method} {url}")

    def run(command, **options):
        calls.append(["run", command[1:]])
        return subprocess.CompletedProcess(command, case.get("lms_exit", 0))

    got = unload_outside(ask_json=ask_json, run=run, find_lms=lambda: "lms" if case.get("lms", True) else None)
    got = [[entry["program"], entry["models"], entry["done"], entry["problem"] is not None] for entry in got]
    failures = []
    if got != case["expect"]:
        failures.append(f"unload_outside: {case['what']}: got {got!r}, expected {case['expect']!r}")
    if "expect_calls" in case and calls != case["expect_calls"]:
        failures.append(f"unload_outside: {case['what']}: called {calls!r}, expected {case['expect_calls']!r}")
    return failures
