"""Where other programs find the Lab: the known address, and the address file a running service writes.

The service listens on 127.0.0.1:8765, the known port, unless told otherwise; when another program has that port, it takes a
free one (serve.bind_service). Every service writes its address, version and process id to one file for this Windows user,
the same wherever the Lab is installed and outside the Lab's folder, unless a running service is named there already, and
removes it when it stops. A file left by a process that has gone is ignored. It also writes the address file every app of
Fel's Studio reads (fels_suite.py). tools/lab_cli.py looks for the Lab in this order: --url, the VOICE_LAB_URL environment
variable, the suite's address file, this file, the known address (find_url).

Standard library only, and Python 3.8 or later, so that lab_cli.py runs on any Python.
"""

import hashlib
import http.client
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

import fels_suite
from app_version import APP_NAME

KNOWN_PORT = 8765
KNOWN_URL = f"http://127.0.0.1:{KNOWN_PORT}"
FOLDER_NAME = "FelsVoiceLabService"
FILE_NAME = "service.json"
SCHEMA = 1
# Beside the Lab: the desktop window's settings, which also name the library it opens (library_backup.use_library).
SETTINGS = "desktop.local.json"
# The name an app gives the Lab's MCP server (tools/mcp_server.py); Claude Code shows its tools as mcp__voice-lab__speak.
MCP_NAME = "voice-lab"


def address_file(environ=None):
    """The address file: VOICE_LAB_ADDRESS_FILE when set (the tests set it), else FelsVoiceLabService\\service.json in this
    user's local application data. Not inside the Lab's folder, so every copy of the Lab and every program agree on it."""
    environ = os.environ if environ is None else environ
    given = environ.get("VOICE_LAB_ADDRESS_FILE")
    if given:
        return Path(given)
    base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / FOLDER_NAME / FILE_NAME


def lab_id(app_root, state_root):
    """A short name for one Lab folder with one library, the same for every service started from them (/api/health's
    lab_id), so the desktop window can tell its own Lab's service from another copy's."""
    text = "\n".join(os.path.normcase(str(Path(path).resolve())) for path in (app_root, state_root))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def process_alive(pid):
    """Whether a process with this id is running now."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
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
            return False
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
    return True


def read(path=None, *, alive=process_alive):
    """The running service the address file names: {"url", "version", "pid", "root", "started_at"}, or None when there is no
    file, it cannot be read, or the process that wrote it has gone."""
    path = Path(path) if path else address_file()
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("schema") != SCHEMA or not isinstance(entry.get("url"), str):
        return None
    return entry if alive(entry.get("pid")) else None


def write(url, *, version, root, pid=None, path=None, alive=process_alive):
    """Name this service in the address file, unless another running service is named there already. True when it is ours."""
    path = Path(path) if path else address_file()
    pid = os.getpid() if pid is None else pid
    current = read(path, alive=alive)
    if current is not None and current.get("pid") != pid:
        return False
    entry = {
        "schema": SCHEMA,
        "url": url,
        "version": version,
        "pid": pid,
        "root": str(root),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="service-", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(handle, "w", encoding="utf-8") as output:
        json.dump(entry, output, indent=2)
    os.replace(temporary, path)
    return True


def remove(*, pid=None, path=None):
    """Take this service's name out of the address file when it stops; another service's stays."""
    path = Path(path) if path else address_file()
    pid = os.getpid() if pid is None else pid
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(entry, dict) or entry.get("pid") != pid:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def find_url(given=None, environ=None, *, default=KNOWN_URL, path=None, alive=process_alive):
    """(the address to use, where it came from): --url, VOICE_LAB_URL, the suite's address file (fels_suite.py), the Lab's
    own address file, or the known address."""
    environ = os.environ if environ is None else environ
    if given:
        return given, "--url"
    if environ.get("VOICE_LAB_URL"):
        return environ["VOICE_LAB_URL"], "VOICE_LAB_URL"
    entry = fels_suite.read_address("voice-lab", environ, alive=alive)
    if entry:
        return entry["url"], "the suite's address file"
    entry = read(path or address_file(environ), alive=alive)
    if entry:
        return entry["url"], "the address file"
    return default, "the known address"


def ask_health(url, timeout=2.0):
    """What answers GET /api/health at `url`, as a dict, or None when nothing does or the answer is not a JSON object."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this computer: no proxy
    try:
        with opener.open(url.rstrip("/") + "/api/health", timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return answer if isinstance(answer, dict) else None


def is_lab(answer):
    """Whether a /api/health answer is a Lab's: this version's says its app; 0.3.0's has its version, status and workers."""
    if not isinstance(answer, dict):
        return False
    return answer.get("app") == APP_NAME or {"version", "status", "workers"} <= set(answer)


def usable_port(value):
    """Whether a port can be the window's: 0 (any free port) or one from 1024 to 65535."""
    return isinstance(value, int) and not isinstance(value, bool) and (value == 0 or 1024 <= value <= 65535)


def window_port(app_root):
    """The port the desktop window's service asks for: "port" in desktop.local.json, else the known port. 0 is any free
    port."""
    try:
        settings = json.loads((Path(app_root) / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return KNOWN_PORT
    value = settings.get("port") if isinstance(settings, dict) else None
    return value if usable_port(value) else KNOWN_PORT


def mcp_settings(app_root, python):
    """How an app starts this Lab's MCP server with the Lab's own Python: the line that adds it to Claude Code for every
    project, and the "mcpServers" entry other MCP apps read from their settings file (docs/CONNECT.md)."""
    script = Path(app_root) / "tools" / "mcp_server.py"
    entry = {"mcpServers": {MCP_NAME: {"command": str(python), "args": [str(script)]}}}
    return {
        "name": MCP_NAME,
        "claude_code": f'claude mcp add --scope user --transport stdio {MCP_NAME} -- "{python}" "{script}"',
        "settings": json.dumps(entry, indent=2),
    }


def window_library(app_root):
    """The library the desktop window opens: "state_root" in desktop.local.json, relative to the Lab's folder, else the
    Lab's folder itself (desktop.main reads it the same way). A program that starts the service for this Lab passes it as
    --state-root, so the window and that service are one Lab (the same lab_id)."""
    app_root = Path(app_root)
    try:
        settings = json.loads((app_root / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return app_root
    value = settings.get("state_root") if isinstance(settings, dict) else None
    return app_root / value if isinstance(value, str) and value else app_root


def save_window_port(app_root, port):
    """Keep the window's port in desktop.local.json, for the next time the window opens; the known port is the default, so
    it is not written. A settings file that is not valid JSON is never overwritten."""
    if not usable_port(port):
        raise ValueError("The port must be 0, for any free port, or a number from 1024 to 65535.")
    path = Path(app_root) / SETTINGS
    settings = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{SETTINGS} cannot be read ({exc}); fix or delete it first.") from exc
        if not isinstance(settings, dict):
            raise ValueError(f"{SETTINGS} is not a settings object; fix or delete it first.")
    if port == KNOWN_PORT:
        settings.pop("port", None)
    else:
        settings["port"] = port
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((json.dumps(settings, indent=2) + "\n").encode("utf-8"))
    os.replace(temporary, path)
    return port


def moved_note(port, taken, answer):
    """Why the service is not on `port`: what answered there (ask_health, or None), and the port it took instead."""
    if is_lab(answer):
        holder = f"Another copy of {APP_NAME} (version {answer.get('version', '?')})"
    else:
        holder = "Another program"
    return f"{holder} is using port {port}, so this Lab listens on port {taken} instead."
