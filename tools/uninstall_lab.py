"""What Setup.exe's uninstaller removes from the Lab's folder: everything but the library, or the library too.

    python\\python.exe tools\\uninstall_lab.py --keep-library
    python\\python.exe tools\\uninstall_lab.py --everything

The uninstaller asks first and keeps the library unless told otherwise (installer/FelsVoiceLab.iss). The library is what a
backup holds (library_backup.SECTIONS), the backups themselves, a restored library, the reference folder and the two
local settings files: LIBRARY. The rest is program: the Lab's files, which update.bat may have added to since the install,
its environments, the models, FFmpeg, logs and caches. A library that desktop.local.json keeps somewhere else is never
touched. Two things are left for the uninstaller itself: the Python this runs on (python\\, removed once it has stopped) and
the uninstaller's own files (unins*). A link or junction is removed as a link; what it points to is never touched.

It runs on the Lab's own Python with nothing installed, so it uses the standard library and the Lab's own modules alone.
"""

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from housekeeping import is_link  # noqa: E402
from library_backup import SECTIONS  # noqa: E402

LIBRARY = tuple(
    sorted(
        {"data", "backups", "restored", "reference", "desktop.local.json", "runtime.local.json"}
        | {folder for section in SECTIONS for folder in section.folders if not folder.startswith("data/")}
    )
)
LEFT_FOR_THE_UNINSTALLER = ("python",)


def _key(path):
    return PurePosixPath(str(path).replace("\\", "/").lower())


def removals(root, *, keep_library):
    """The paths under root to remove, and the folders opened because they hold part of the library (innermost first): an
    opened folder is removed afterwards when nothing was kept in it."""
    root = Path(root)
    keep = [_key(item) for item in LIBRARY] if keep_library else []
    found, opened = [], []

    def visit(folder, relative):
        for entry in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
            name = relative / entry.name
            key = _key(name)
            if relative == PurePosixPath(".") and (
                entry.name.lower() in LEFT_FOR_THE_UNINSTALLER or entry.name.lower().startswith("unins")
            ):
                continue
            if any(key == kept or kept in key.parents for kept in keep):
                continue
            if entry.is_dir() and not is_link(entry) and any(key in kept.parents for kept in keep):
                visit(entry, name)
                opened.append(entry)
                continue
            found.append(entry)

    visit(root, PurePosixPath("."))
    return found, opened


def _writable(function, path, _info):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove(path):
    """Remove one file, folder or link. A link or junction goes; what it points to stays."""
    if is_link(path):
        os.rmdir(path) if path.is_dir() else path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, onexc=_writable)
    else:
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink()


def run(root, *, keep_library, say=print):
    failed = []
    found, opened = removals(root, keep_library=keep_library)
    for path in found:
        try:
            remove(path)
        except OSError as exc:
            failed.append(f"{path.relative_to(root)} ({exc.strerror or exc})")
    for folder in opened:
        if not any(folder.iterdir()):
            folder.rmdir()
    if keep_library:
        kept = [item for item in LIBRARY if (Path(root) / item).exists()]
        say(f"Kept the library in {root}: {', '.join(kept) or 'nothing was there yet'}.")
    else:
        say(f"Removed the library in {root} too.")
    if failed:
        say("Could not remove, probably because a program still has them open: " + "; ".join(failed) + ".")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Remove the Lab from its folder, keeping the library or not.")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--keep-library", action="store_true", help="remove the program and keep the library")
    choice.add_argument("--everything", action="store_true", help="remove the library too")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return run(args.root, keep_library=args.keep_library)


if __name__ == "__main__":
    sys.exit(main())
