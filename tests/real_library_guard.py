"""Keep the test run away from the real library.

The tests build their own libraries in temporary folders. Importing this module installs an audit hook for the rest of the
process (tests/test_00_real_library_guard.py imports it, and unittest imports test files in name order, so it is in place
before any test runs). The hook refuses, with a message that says what to do instead, any attempt to write under the places
that hold the user's work or to open one of its databases:

    data/                                  Cast, Scenes, Comparisons, saved voices, appearance, engine checks
    out/scenes, out/comparisons, out/m5,   what the library backup treats as library content
    out/exports, out/drama3
    desktop.local.json                     which library the desktop app opens (and the temporary file that replaces it)

Reading is allowed. Folders outside that list, such as out/workers, are scratch space and are not guarded. A child process
started by a test is not covered; give it a temporary --state-root.
"""
import os
import socketserver
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

LAB = Path(__file__).resolve().parents[1]
# out/m5 is the old name of out/comparisons, still guarded for a library that has not been opened since the rename.
FOLDERS = tuple(LAB / name for name in ("data", "out/scenes", "out/comparisons", "out/m5", "out/exports", "out/drama3"))
CONFIG = LAB / "desktop.local.json"

_folders = tuple(os.path.normcase(str(path)) for path in FOLDERS)
_config = os.path.normcase(str(CONFIG))
_installed = False
_WHAT = {"open": "write to", "os.mkdir": "create the folder", "os.rmdir": "remove the folder", "os.remove": "delete",
         "os.rename": "rename", "sqlite3.connect": "open the database"}


class RealLibraryError(BaseException):
    """A test reached the real library. A BaseException on purpose: a service's catch-all `except Exception` that answers 500
    must not be able to turn this into a quiet failure."""


def _text(value):
    """A file name as text, or None for anything that is not one (a file descriptor, an in-memory database)."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value or value == ":memory:":
        return None
    if value.startswith("file:"):                       # a SQLite URI
        text = unquote(urlparse(value).path)
        value = text[1:] if len(text) > 2 and text[0] == "/" and text[2] == ":" else text
    return value


def protected(path):
    """The guarded folder or file that `path` lies in, as text, or None."""
    name = _text(path)
    if name is None:
        return None
    try:
        full = os.path.normcase(os.path.abspath(name))
    except (ValueError, OSError):
        return None
    for folder in _folders:
        if full == folder or full.startswith(folder + os.sep):
            return folder
    if os.path.dirname(full) == os.path.dirname(_config) and os.path.basename(full).startswith(os.path.basename(_config)):
        return _config
    return None


def _writes(flags):
    """Whether an open() would write or create. The flags are always given, whether the file is opened by io.open (which
    works them out from its mode) or by os.open."""
    return isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _hook(event, args):
    if event == "open":
        if not _writes(args[2]):
            return
        names = (args[0],)
    elif event in ("os.mkdir", "os.rmdir", "os.remove"):
        names = (args[0],)
    elif event == "os.rename":
        names = (args[0], args[1])
    elif event == "sqlite3.connect":
        names = (args[0],)
    else:
        return
    for name in names:
        found = protected(name)
        if found is not None:
            shown = _text(name)
            raise RealLibraryError(
                f"A test tried to {_WHAT[event]} {shown}, inside the real library ({found}). Give the test a temporary library: "
                "CastStore(tmp), then serve.make_handler(engines, port, cast), or library_fixture.temp_cast(self).")


def _let_shutdown_return_before_serving():
    """socketserver's shutdown() waits for a serve_forever() that never began, and so waits for ever. A test whose service
    failed to build would then hang in tearDown instead of reporting the failure. A server that is not serving is already shut
    down: mark it so, and serve_forever() clears the mark when it starts."""
    original = socketserver.BaseServer.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        event = getattr(self, "_BaseServer__is_shut_down", None)
        if event is not None:
            event.set()

    socketserver.BaseServer.__init__ = init


def install():
    """Put the guard in place for this process. False when it already is."""
    global _installed
    if _installed:
        return False
    _installed = True
    sys.addaudithook(_hook)
    _let_shutdown_return_before_serving()
    return True


install()
