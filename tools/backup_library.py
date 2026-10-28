"""Back up the library to one ZIP, check a backup, or restore one into a new folder.

    .venv\\Scripts\\python.exe tools\\backup_library.py create
    .venv\\Scripts\\python.exe tools\\backup_library.py create --to D:\\Backups --without exports drama
    .venv\\Scripts\\python.exe tools\\backup_library.py verify backups\\voice-lab-library-20261025-120000.zip
    .venv\\Scripts\\python.exe tools\\backup_library.py restore backups\\voice-lab-library-20261025-120000.zip D:\\VoiceLab-restored --use
    .venv\\Scripts\\python.exe tools\\backup_library.py use D:\\VoiceLab-restored

`create` writes the ZIP into <library>\\backups (or --to), reads it back and checks every file against its checksum.
`restore` never writes into an existing library: it builds a checked copy in the new folder, and only then gives the folder
its name. `--use`, or `use` later, points the desktop app at a library folder the next time it starts (it writes
desktop.local.json); without it nothing else changes, and `use` with the folder you came from switches back. The library is
the folder the desktop app opens: desktop.local.json's state_root, else this project, or whatever --state-root names. Stop
the service first if you can; a running one is copied consistently, but a take that is half way through saving is not.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library_backup  # noqa: E402
from app_version import APP_NAME, VERSION  # noqa: E402


def _progress(label):
    """A progress callback for a terminal: a line about every two seconds. Nothing when the output is piped."""
    if not sys.stderr.isatty():
        return None
    last = [0.0]

    def show(files_done, files_total, bytes_done, bytes_total):
        now = time.monotonic()
        if now - last[0] >= 2 or files_done == files_total:
            last[0] = now
            print(f"  {label}: {files_done} of {files_total} files, {library_backup.format_bytes(bytes_done)} of "
                  f"{library_backup.format_bytes(bytes_total)}", file=sys.stderr)
    return show


def _show(outcome):
    if outcome["ok"]:
        print(f"{outcome['name']}: every file matches its checksum and each database passes SQLite's integrity check "
              f"({outcome['files']} files, {library_backup.format_bytes(outcome['content_bytes'])}).")
        return 0
    print(f"{outcome['name']} did not pass:")
    for problem in outcome["problems"]:
        print(f"  {problem}")
    return 1


def _switch(switched):
    print(f"The desktop app opens {switched['state_root']} from its next start ({switched['settings_file']}).")
    print(f"  Before this it opened {switched['previous'] or 'this project'}; `use` with that folder switches back.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="write a backup ZIP and check it")
    create.add_argument("--state-root", type=Path, help="the library to back up (default: the one the desktop app opens)")
    create.add_argument("--to", type=Path, help="the folder to write into (default: <library>\\backups)")
    create.add_argument("--without", nargs="*", default=[], choices=library_backup.OPTIONAL, metavar="SECTION",
                        help="leave out optional sections: " + ", ".join(library_backup.OPTIONAL))
    verify = commands.add_parser("verify", help="read a backup in full and check it")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore", help="build a checked copy of a backup in a new folder")
    restore.add_argument("backup", type=Path)
    restore.add_argument("folder", type=Path, help="a folder that does not exist yet, or is empty")
    restore.add_argument("--use", action="store_true", help="open the restored library the next time the desktop app starts")
    use = commands.add_parser("use", help="open a library folder the next time the desktop app starts")
    use.add_argument("folder", type=Path, help="a folder that holds a library (it has data\\cast\\cast.sqlite3)")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            library = (args.state_root or library_backup.configured_state_root(ROOT)).resolve()
            destination = args.to or library / library_backup.BACKUP_FOLDER
            include = [key for key in library_backup.OPTIONAL if key not in args.without]
            result = library_backup.create(library, destination, include=include, app=APP_NAME, version=VERSION,
                                           progress=_progress("backing up"))
            print(f"Backed up {library}")
            print(f"  {result['path']}")
            print(f"  {result['files']} files, {library_backup.format_bytes(result['bytes'])}; read back and checked.")
            for warning in result["warnings"]:
                print(f"  note: {warning}")
            return 0
        if args.command == "verify":
            return _show(library_backup.verify(args.backup, progress=_progress("checking")))
        if args.command == "use":
            _switch(library_backup.use_library(ROOT, args.folder))
            return 0
        result = library_backup.restore(args.backup, args.folder, progress=_progress("restoring"))
        counts = result["counts"]
        print(f"Restored to {result['state_root']}")
        print(f"  {result['files']} files, {counts['characters']} characters, {counts['takes']} takes; every file matched its "
              "checksum and the databases opened. The library in use was not touched.")
        for warning in result["warnings"]:
            print(f"  note: {warning}")
        if args.use:
            _switch(library_backup.use_library(ROOT, result["state_root"]))
        else:
            print(f"  To look at it now, start the service on it: .venv\\Scripts\\python.exe serve.py --state-root \"{result['state_root']}\"")
            print(f"  To open it in the desktop app: .venv\\Scripts\\python.exe tools\\backup_library.py use \"{result['state_root']}\"")
        return 0
    except (library_backup.BackupError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
