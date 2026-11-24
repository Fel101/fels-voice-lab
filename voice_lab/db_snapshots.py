"""A copy of a database file, taken just before its schema moves up, so a migration that goes wrong has a way back.

Cast, Scenes and Evaluations each keep their schema number in SQLite's user_version. The first open under a newer
build changes the file in place and an older build cannot read it afterwards. The copy goes into a migration-backups
folder beside the database, and the newest few are kept.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

FOLDER = "migration-backups"
KEEP = 3


class SnapshotError(RuntimeError):
    pass


def snapshot_before_migration(db, db_path, version, latest, label):
    """Copy the database when it holds an older schema than this build writes (a new, empty file has nothing to lose).
    `db` is the open connection that is about to migrate it. The copy is consistent even while another process writes.
    Returns the copy's path, or None when nothing needed copying. If the copy cannot be made the migration must not
    go ahead, so this raises."""
    if not 0 < version < latest:
        return None
    folder = Path(db_path).parent / FOLDER
    target = folder / f"{label}-schema{version}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.sqlite3"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        copy = sqlite3.connect(target)
        try:
            db.backup(copy)
        finally:
            copy.close()
    except (OSError, sqlite3.Error) as exc:
        target.unlink(missing_ok=True)
        raise SnapshotError(f"Could not copy the {label} database to {folder} before updating it to schema {latest}: "
                            f"{exc}. Nothing was changed.") from exc
    _prune(folder, label)
    return target


def _prune(folder, label):
    copies = sorted(folder.glob(f"{label}-schema*-*.sqlite3"), key=lambda path: (path.stat().st_mtime, path.name))
    for old in copies[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
