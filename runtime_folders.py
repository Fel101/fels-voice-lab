"""Folders that earlier builds named after development milestones, and the one-time move to the names used now.

Nothing here runs by itself. serve.py calls migrate_program_folders() as it starts, and the evaluation store calls
move_folder() when it brings an older database forward.

A move is a rename inside one drive, so it is quick and cannot stop half way. It never merges two folders and never
overwrites one: if the new name already holds anything, the old folder stays where it is and the caller says so.
"""

from pathlib import Path
import os

from housekeeping import is_link

# Inside out/, where a library keeps the audio of its comparisons. Its database stores paths that start with the folder's
# name, so the evaluation store rewrites them when it moves the folder.
COMPARISONS_DIR = "comparisons"
LEGACY_COMPARISONS_DIR = "m5"

# Work worth keeping that earlier builds left under the program folder, as (old, new) paths. Worker logs are not listed:
# they are disposable, so the ones left in out/m1, out/m2b and out/m6a stay where they are and new ones go to out/logs/workers.
PROGRAM_MOVES = (
    ("out/m7/conversions", "out/conversions"),
    ("out/m6/source", "out/voxcpm2/source"),
    ("out/m11/datasets", "out/datasets"),
)


class FolderMoveError(RuntimeError):
    """A folder that could not take its new name. The message says why in plain words."""


def move_folder(source, target):
    """Rename the folder `source` to `target`. True when it moved, False when there was no such folder. An empty folder
    already at `target` is replaced; anything else there raises FolderMoveError and changes nothing."""
    source, target = Path(source), Path(target)
    if not source.is_dir():
        return False
    if os.path.lexists(target):
        if target.is_dir() and not is_link(target) and not any(target.iterdir()):
            target.rmdir()
        else:
            raise FolderMoveError(f"{source} should be renamed to {target}, but {target} is already there. Move what is in "
                                  f"{source.name} into it by hand, delete {source.name}, then start again.")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(target)
    except OSError as exc:
        raise FolderMoveError(f"{source} could not be renamed to {target}: {exc}. Close any program that has a file in "
                              "it open, then start again.") from exc
    return True


def migrate_program_folders(root):
    """Move each folder in PROGRAM_MOVES that an earlier build left under `root`. Returns one plain sentence for every
    folder that moved or could not (an empty list when there was nothing to do); one that cannot move does not stop the rest."""
    notes = []
    for old, new in PROGRAM_MOVES:
        try:
            if move_folder(Path(root) / old, Path(root) / new):
                notes.append(f"moved {old} to {new}")
        except FolderMoveError as exc:
            notes.append(f"did not move {old} to {new}: {exc}")
    return notes
