"""A one-file backup of the library, and a restore that builds a checked copy in a new folder.

The library is what the user guide says to keep together: the Cast, Scenes and Evaluations databases with their media
(data/cast, data/scenes, data/evaluations, out/scenes, out/comparisons) and the appearance setting. Saved voices, export
packages and Drama studio jobs can be added. Model caches, environments, logs, voice conversions and the safety copies in
migration-backups are not part of it.

A backup is one ZIP. BACKUP_MANIFEST.json lists every other member with its size and SHA-256, the schema number of each
database and what was left out. The databases go in as SQLite online-backup copies, so they are consistent while the
service runs. Scenes and Evaluations are copied before Cast because they point at Cast's takes, and a Cast copy made last
can only know more than they do.

Restoring never touches the library in use. It unpacks into a new folder beside the final one, checks every file against
the manifest, runs SQLite's integrity check on each database, opens the stores (which moves an older schema forward) and
looks for media the databases refer to; only then does it rename the folder into place. Pointing the app at the new folder
is a separate step, use_library().
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from housekeeping import is_link
from runtime_folders import COMPARISONS_DIR, LEGACY_COMPARISONS_DIR

FORMAT = "fels-voice-lab-library-backup"
FORMAT_VERSION = 1
MANIFEST = "BACKUP_MANIFEST.json"
MEMBER_ROOT = "library/"
BACKUP_FOLDER = "backups"
RESTORE_FOLDER = "restored"
NAME = re.compile(r"voice-lab-library-\d{8}-\d{6}(?:-\d+)?\.zip\Z")
PART = re.compile(r"voice-lab-library-\d{8}-\d{6}(?:-\d+)?\.zip\.part\Z")
SCRATCH = re.compile(r"\.backup-[A-Za-z0-9_]+\Z")
FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}\Z")
CHUNK = 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
MAX_PROBLEMS = 12
MAX_NOTES = 200
MAX_LISTED = 25
STALE_SECONDS = 86400
MARGIN_BYTES = 64 * 1024 * 1024
STORED_SUFFIXES = {".flac", ".mp3", ".ogg", ".m4a", ".zip", ".png", ".jpg", ".gguf"}  # already compressed: stored as they are
SKIP_FOLDERS = {"migration-backups", "__pycache__"}
SKIP_SUFFIXES = (".lock", ".tmp", ".part", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")
RESERVED_NAME = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    folders: tuple
    optional: bool = False


SECTIONS = (
    Section("cast", "Cast voices, references and takes", ("data/cast",)),
    Section("scenes", "Scenes and rendered lines", ("data/scenes", "out/scenes")),
    # A library that has not been opened since the comparison folder was renamed still has the old name, so both are listed:
    # its backup holds whichever it has, and a backup made before the rename restores and is moved when the store opens.
    Section("evaluations", "Comparisons and ratings",
            ("data/evaluations", f"out/{COMPARISONS_DIR}", f"out/{LEGACY_COMPARISONS_DIR}")),
    Section("settings", "Appearance setting", ("data/ui",)),
    Section("voices", "Saved voices", ("data/voices",), True),
    Section("exports", "Export packages", ("out/exports",), True),
    Section("drama", "Drama studio jobs", ("out/drama3/studio",), True),
)
OPTIONAL = tuple(section.key for section in SECTIONS if section.optional)
ALLOWED_PREFIXES = tuple(folder + "/" for section in SECTIONS for folder in section.folders)
# The order the databases are copied in: see the module note.
DATABASES = {"evaluations": "data/evaluations/evaluations.sqlite3", "scenes": "data/scenes/scenes.sqlite3",
             "cast": "data/cast/cast.sqlite3"}
DATABASE_LABEL = {"cast": "Cast", "scenes": "Scenes", "evaluations": "Evaluations"}


class BackupError(ValueError):
    """A backup or restore that cannot go ahead or did not check out. The message is plain words, safe to show."""


class BackupBusy(BackupError):
    pass


def format_bytes(count):
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def _stamp(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, timezone.utc).isoformat(timespec="seconds")


def chosen_sections(include):
    """The section keys a backup holds: the required ones and the optional ones asked for."""
    asked = set(include or ())
    unknown = sorted(asked - set(OPTIONAL))
    if unknown:
        raise BackupError(f"Unknown backup section: {', '.join(unknown)}. Optional sections are {', '.join(OPTIONAL)}.")
    return {section.key for section in SECTIONS if not section.optional} | asked


def _walk(root, folder, skip_files, skipped):
    """Every regular file under root/folder as (posix path relative to root, file), in a fixed order. Links are not
    followed, and what is left out for being one is noted in `skipped`."""
    top = root / folder
    if not top.is_dir():
        return
    if is_link(top):
        skipped.append({"path": folder, "reason": "it is a link, and links are not followed"})
        return
    for directory, folders, names in os.walk(top):
        here = Path(directory)
        keep = []
        for name in sorted(folders):
            if name in SKIP_FOLDERS:
                continue
            if is_link(here / name):
                skipped.append({"path": (here / name).relative_to(root).as_posix(),
                                "reason": "it is a link, and links are not followed"})
                continue
            keep.append(name)
        folders[:] = keep
        for name in sorted(names):
            path = here / name
            relative = path.relative_to(root).as_posix()
            if relative in skip_files or name.lower().endswith(SKIP_SUFFIXES):
                continue
            if is_link(path):
                skipped.append({"path": relative, "reason": "it is a link, and links are not followed"})
                continue
            yield relative, path


def plan(state_root, include=OPTIONAL):
    """What a backup holds, by section, from file sizes alone: files and bytes for each, whether it is included, and
    the totals for the included ones."""
    root = Path(state_root).resolve()
    chosen = chosen_sections(include)
    live = {relative: key for key, relative in DATABASES.items()}
    rows, files, size = [], 0, 0
    for section in SECTIONS:
        count = total = 0
        for folder in section.folders:
            for _relative, path in _walk(root, folder, set(live), []):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
                count += 1
        for relative, key in live.items():
            if key == section.key and (root / relative).is_file():
                count += 1
                total += (root / relative).stat().st_size
        included = section.key in chosen
        rows.append({"key": section.key, "label": section.label, "optional": section.optional, "included": included,
                     "files": count, "bytes": total})
        if included:
            files, size = files + count, size + total
    return {"sections": rows, "files": files, "bytes": size}


def _snapshot(source, target, label):
    """A consistent copy of a database that may be in use (SQLite's online backup). Returns its schema number. A copy
    that fails SQLite's own integrity check is refused: a backup that looks fine and is not is worse than none."""
    reader = sqlite3.connect(source, timeout=30)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
            version = writer.execute("PRAGMA user_version").fetchone()[0]
            verdict = [row[0] for row in writer.execute("PRAGMA integrity_check")]
        finally:
            writer.close()
    except sqlite3.Error as exc:
        raise BackupError(f"The {label} database could not be copied: {exc}. Nothing was changed.") from exc
    finally:
        reader.close()
    if verdict != ["ok"]:
        raise BackupError(f"The {label} database fails SQLite's integrity check ({verdict[0]}), so it was not backed up. "
                          "Copy the folders by hand if you need what is left of it, and do not rely on this database.")
    return version


def _referenced_media(db_path):
    """Relative paths of the media files a Cast database refers to, or None when it cannot be read that way."""
    try:
        db = sqlite3.connect(db_path)
        try:
            found = set()
            for query in ("SELECT source_path FROM reference_versions", "SELECT audio_path FROM reference_versions",
                          "SELECT audio_path FROM takes"):
                found.update(row[0] for row in db.execute(query))
        finally:
            db.close()
    except sqlite3.Error:
        return None
    return sorted(path for path in found if path)


def _add(archive, source, member, relative, report):
    """Write one file into the archive, hashing it on the way. Returns its manifest entry."""
    info = zipfile.ZipInfo.from_file(source, member, strict_timestamps=False)
    info.compress_type = zipfile.ZIP_STORED if source.suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as handle, archive.open(info, "w", force_zip64=info.file_size >= zipfile.ZIP64_LIMIT) as target:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
            target.write(chunk)
            size += len(chunk)
            report(len(chunk))
    return {"path": relative, "size": size, "sha256": digest.hexdigest()}


def _free_name(folder, stamp):
    name = f"voice-lab-library-{stamp}.zip"
    count = 1
    while (folder / name).exists() or (folder / (name + ".part")).exists():
        count += 1
        name = f"voice-lab-library-{stamp}-{count}.zip"
    return name


def _clear_stale(folder):
    """Remove what a backup that was cut short (the service stopped half way) left in its folder, once it is a day old."""
    cutoff = time.time() - STALE_SECONDS
    for entry in folder.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_file() and PART.fullmatch(entry.name):
                entry.unlink()
            elif entry.is_dir() and SCRATCH.fullmatch(entry.name) and not is_link(entry):
                shutil.rmtree(entry)
        except OSError:
            pass


class _Progress:
    """Counts files and bytes done against the totals and passes them on."""

    def __init__(self, files, size, callback):
        self.files, self.size, self.callback = files, size, callback
        self.files_done = self.bytes_done = 0

    def __call__(self, added=0, file_done=False):
        self.bytes_done += added
        self.files_done += 1 if file_done else 0
        if self.callback is not None:
            self.callback(self.files_done, self.files, self.bytes_done, self.size)


def create(state_root, destination, *, include=OPTIONAL, app="", version="", progress=None):
    """Write a backup of the library under `state_root` into the folder `destination`, read it back and check it, and
    return what was written: {"path", "name", "bytes", "files", "created_at", "sections", "databases", "warnings"}.
    `progress(files_done, files_total, bytes_done, bytes_total)` is called as it goes."""
    root = Path(state_root).resolve()
    destination = Path(destination).resolve()
    chosen = chosen_sections(include)
    if not (root / DATABASES["cast"]).is_file():
        raise BackupError("There is nothing to back up yet: this library has no Cast database.")
    for section in SECTIONS:
        for folder in section.folders:
            inside = (root / folder).resolve()
            if section.key in chosen and (destination == inside or inside in destination.parents):
                raise BackupError(f"The backup folder {destination} is inside {folder}, which the backup copies. "
                                  "Choose a folder outside the library folders.")
    destination.mkdir(parents=True, exist_ok=True)
    _clear_stale(destination)
    expected = plan(root, include)
    needed, free = expected["bytes"] + MARGIN_BYTES, shutil.disk_usage(destination).free
    if free < needed:
        raise BackupError(f"There is not enough free space for the backup: it needs about {format_bytes(needed)} and "
                          f"{format_bytes(free)} is free where it would be written.")
    name = _free_name(destination, datetime.now().strftime("%Y%m%d-%H%M%S"))
    target, part = destination / name, destination / (name + ".part")
    scratch = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination))
    report = _Progress(expected["files"], expected["bytes"], progress)
    entries, skipped, databases, tally = [], [], {}, {}
    live = set(DATABASES.values())

    def put(archive, source, relative, section):
        entry = _add(archive, source, MEMBER_ROOT + relative, relative, report)
        entries.append(entry)
        row = tally.setdefault(section, {"files": 0, "bytes": 0})
        row["files"] += 1
        row["bytes"] += entry["size"]
        report(file_done=True)

    try:
        with zipfile.ZipFile(part, "w", allowZip64=True) as archive:
            for key, relative in DATABASES.items():
                source = root / relative
                if source.is_file():
                    copy = scratch / f"{key}.sqlite3"
                    databases[key] = {"path": relative, "schema_version": _snapshot(source, copy, DATABASE_LABEL[key])}
                    put(archive, copy, relative, key)
            for section in SECTIONS:
                if section.key not in chosen:
                    continue
                for folder in section.folders:
                    for relative, path in _walk(root, folder, live, skipped):
                        try:
                            put(archive, path, relative, section.key)
                        except FileNotFoundError:
                            skipped.append({"path": relative, "reason": "it was removed while the backup ran"})
            referenced = _referenced_media(scratch / "cast.sqlite3")
            missing = None if referenced is None else [path for path in referenced if not (root / path).is_file()]
            manifest = {
                "format": FORMAT, "format_version": FORMAT_VERSION, "app": app, "app_version": version,
                "created_at": _stamp(), "library_name": root.name, "included": sorted(chosen),
                "sections": {section.key: {"label": section.label, **tally.get(section.key, {"files": 0, "bytes": 0})}
                             for section in SECTIONS if section.key in chosen},
                "databases": databases, "files": entries,
                "skipped": skipped[:MAX_NOTES], "skipped_total": len(skipped),
                "media_checked": missing is not None,
                "missing_media": (missing or [])[:MAX_NOTES], "missing_media_total": len(missing or []),
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True))
        outcome = verify(part)
        if not outcome["ok"]:
            raise BackupError("The backup was written but did not check out, so it was discarded: "
                              + "; ".join(outcome["problems"][:3]))
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    warnings = []
    if skipped:
        warnings.append(f"{len(skipped)} items were left out: {skipped[0]['path']} ({skipped[0]['reason']})"
                        + (" and others." if len(skipped) > 1 else "."))
    if manifest["missing_media_total"]:
        warnings.append(f"{manifest['missing_media_total']} media files that the Cast database refers to were already missing "
                        f"from the library, for example {manifest['missing_media'][0]}.")
    return {"path": str(target), "name": name, "bytes": target.stat().st_size, "files": len(entries),
            "created_at": manifest["created_at"], "sections": manifest["sections"], "databases": databases,
            "warnings": warnings}


def _safe_path(text):
    """A manifest path that names a file inside the library folders and nowhere else (never an absolute path, a drive,
    a parent folder or a name Windows treats as a device)."""
    if not isinstance(text, str) or not text or len(text) > 400 or "\\" in text or ":" in text or text.startswith("/"):
        raise BackupError(f"The backup lists a file name that is not allowed: {str(text)[:80]!r}.")
    for part in text.split("/"):
        if (part in ("", ".", "..") or part != part.strip() or part.endswith(".") or RESERVED_NAME.match(part)
                or any(ord(char) < 32 for char in part)):
            raise BackupError(f"The backup lists a file name that is not allowed: {text[:80]!r}.")
    if not text.startswith(ALLOWED_PREFIXES):
        raise BackupError(f"The backup lists a file outside the library folders: {text[:80]!r}.")
    return text


def _read_manifest(archive, *, light=False):
    """The manifest, parsed and (unless `light`) checked file by file for shape and for safe names."""
    infos = [info for info in archive.infolist() if info.filename == MANIFEST]
    if len(infos) != 1:
        raise BackupError(f"This file is not a Fel's Voice Lab library backup: it has no {MANIFEST}.")
    if infos[0].file_size > MAX_MANIFEST_BYTES:
        raise BackupError("This file is not a Fel's Voice Lab library backup: its manifest is far too large.")
    try:
        manifest = json.loads(archive.read(infos[0]).decode("utf-8"))
    except (ValueError, zipfile.BadZipFile, zlib.error) as exc:
        raise BackupError(f"The backup's manifest cannot be read: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("This file is not a Fel's Voice Lab library backup.")
    version = manifest.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BackupError("This backup does not say which format it uses.")
    if version > FORMAT_VERSION:
        raise BackupError(f"This backup was made by a newer version of the app (backup format {version}); this build reads "
                          f"format {FORMAT_VERSION}. Update the app, then try again.")
    if not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("databases"), dict):
        raise BackupError("The backup's manifest is incomplete.")
    if light:
        return manifest
    seen = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise BackupError("The backup's manifest is damaged.")
        path = _safe_path(entry.get("path"))
        size, digest = entry.get("size"), entry.get("sha256")
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(digest, str) or not SHA256.match(digest)):
            raise BackupError(f"The backup's manifest has a bad size or checksum for {path}.")
        if path in seen:
            raise BackupError(f"The backup lists {path} twice.")
        seen.add(path)
    for key, record in manifest["databases"].items():
        if (key not in DATABASES or not isinstance(record, dict) or record.get("path") != DATABASES[key]
                or not isinstance(record.get("schema_version"), int) or DATABASES[key] not in seen):
            raise BackupError(f"The backup's manifest describes the {key} database wrongly.")
    if "cast" not in manifest["databases"]:
        raise BackupError("This backup holds no Cast database.")
    return manifest


def _read_members(archive, manifest, sink=None, report=None):
    """Read every listed member in full and compare it with the manifest; with `sink`, hand each file's chunks on for
    writing. Returns the list of problems, in plain words."""
    problems = []
    names = [info.filename for info in archive.infolist() if info.filename != MANIFEST and not info.filename.endswith("/")]
    counts = Counter(names)
    listed = {MEMBER_ROOT + entry["path"]: entry for entry in manifest["files"]}
    for name in sorted(listed):
        if name not in counts:
            problems.append(f"{name[len(MEMBER_ROOT):]} is listed but missing from the backup.")
    for name in sorted(counts):
        if name not in listed:
            problems.append(f"{name} is in the backup file but is not listed in its manifest.")
        elif counts[name] > 1:
            problems.append(f"{name[len(MEMBER_ROOT):]} appears more than once in the backup file.")
    for entry in manifest["files"]:
        path, member = entry["path"], MEMBER_ROOT + entry["path"]
        if counts.get(member) != 1:
            continue
        info = archive.getinfo(member)
        if info.file_size != entry["size"]:
            problems.append(f"{path} has the wrong size ({info.file_size} bytes, expected {entry['size']}).")
            continue
        digest, size = hashlib.sha256(), 0
        try:
            with archive.open(info) as stream:
                writer = sink(path) if sink else None
                try:
                    for chunk in iter(lambda: stream.read(CHUNK), b""):
                        size += len(chunk)
                        if size > entry["size"]:
                            break
                        digest.update(chunk)
                        if writer:
                            writer.write(chunk)
                        if report:
                            report(len(chunk))
                finally:
                    if writer:
                        writer.close()
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
            problems.append(f"{path} cannot be read from the backup: {exc}.")
            continue
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            problems.append(f"{path} does not match its checksum.")
        if report:
            report(0, True)
    return problems


def _database_problems(path, label, record):
    """Problems with one unpacked database: SQLite's integrity check, and a schema other than the one the manifest names."""
    try:
        db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        try:
            verdict = [row[0] for row in db.execute("PRAGMA integrity_check")]
            version = db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error as exc:
        return [f"The {label} database in the backup cannot be opened: {exc}."]
    problems = []
    if verdict != ["ok"]:
        problems.append(f"The {label} database in the backup fails SQLite's integrity check ({verdict[0]}).")
    if version != record["schema_version"]:
        problems.append(f"The {label} database is at schema {version}, but the manifest says {record['schema_version']}.")
    return problems


def _limit(problems):
    if len(problems) > MAX_PROBLEMS:
        return problems[:MAX_PROBLEMS] + [f"... and {len(problems) - MAX_PROBLEMS} more."]
    return problems


def verify(zip_path, *, deep=True, progress=None):
    """Read a backup in full and compare it with its manifest. Never raises for a damaged file: returns {"ok", "problems",
    "name"} and, when the manifest could be read, its summary. `deep` also runs SQLite's integrity check on each database."""
    path = Path(zip_path)
    result = {"ok": False, "problems": [], "name": path.name}
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = _read_manifest(archive)
            report = _Progress(len(manifest["files"]), sum(entry["size"] for entry in manifest["files"]), progress)
            problems = _read_members(archive, manifest, report=report)
            if deep and not problems:
                with tempfile.TemporaryDirectory(prefix="voice-lab-verify-") as scratch:
                    for key, record in manifest["databases"].items():
                        copy = Path(scratch) / f"{key}.sqlite3"
                        with archive.open(MEMBER_ROOT + record["path"]) as stream, copy.open("wb") as handle:
                            shutil.copyfileobj(stream, handle, CHUNK)
                        problems += _database_problems(copy, DATABASE_LABEL[key], record)
            result.update(summary(manifest), problems=_limit(problems), ok=not problems)
    except BackupError as exc:
        result["problems"] = [str(exc)]
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        result["problems"] = [f"The file cannot be read as a backup: {exc}"]
    return result


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _words(value):
    return value[:100] if isinstance(value, str) else None


def summary(manifest):
    """What a person wants to know about a backup, from its manifest, with the sections in their usual order. A manifest
    written by something else may hold odd values, so each is checked: a listing must not fail on one file in the folder."""
    held = manifest.get("sections") if isinstance(manifest.get("sections"), dict) else {}
    included = manifest.get("included") if isinstance(manifest.get("included"), list) else []
    sections, databases = {}, {}
    for section in SECTIONS:
        row = held.get(section.key)
        if isinstance(row, dict):
            sections[section.key] = {"label": section.label, "files": _whole(row.get("files")), "bytes": _whole(row.get("bytes"))}
    for key in DATABASES:
        record = manifest["databases"].get(key)
        if isinstance(record, dict):
            version = record.get("schema_version")
            databases[key] = version if isinstance(version, int) and not isinstance(version, bool) else None
    return {"created_at": _words(manifest.get("created_at")), "app_version": _words(manifest.get("app_version")),
            "library_name": _words(manifest.get("library_name")), "files": len(manifest["files"]),
            "content_bytes": sum(_whole(entry.get("size")) for entry in manifest["files"] if isinstance(entry, dict)),
            "included": [section.key for section in SECTIONS if section.key in included], "sections": sections,
            "databases": databases, "skipped": _whole(manifest.get("skipped_total")),
            "missing_media": _whole(manifest.get("missing_media_total"))}


_listing_cache = {}


def list_backups(folder):
    """The backups in a folder, newest first. Each carries its size and, when its manifest reads, its summary; a file
    that does not read says so. Nothing is hashed here."""
    folder = Path(folder)
    rows = []
    if not folder.is_dir():
        return rows
    for path in folder.iterdir():
        try:
            if not (path.is_file() and NAME.fullmatch(path.name)):
                continue
            stat = path.stat()
        except OSError:
            continue
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _listing_cache.get(key)
        if cached is None:
            try:
                with zipfile.ZipFile(path) as archive:
                    cached = summary(_read_manifest(archive, light=True))
            except (BackupError, zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
                cached = {"problem": str(exc)}
            _listing_cache[key] = cached
        rows.append({"name": path.name, "bytes": stat.st_size, "modified": _stamp(stat.st_mtime), **cached})
    rows.sort(key=lambda row: row["name"], reverse=True)
    return rows[:MAX_LISTED]


def backup_path(folder, name):
    """The file for a backup name a caller supplied. Only a plain backup file name in `folder` is accepted."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise BackupError("That is not the name of a backup.")
    path = Path(folder) / name
    if not path.is_file():
        raise BackupError(f"There is no backup called {name}.")
    return path


def restore_target(library, name):
    """Where a restore with this folder name goes: restored/<name> inside the library. The name is plain words and
    never the name of something that already exists."""
    if (not isinstance(name, str) or not FOLDER_NAME.fullmatch(name) or name != name.strip() or name.endswith(".")
            or RESERVED_NAME.match(name)):
        raise BackupError("Use letters, digits, spaces, dots, dashes and underscores for the folder name, up to 60 characters.")
    return Path(library) / RESTORE_FOLDER / name


def suggested_folder(library):
    """A folder name for the next restore that is not taken yet."""
    base = f"{Path(library).name}-restored-{datetime.now().strftime('%Y%m%d')}"
    name, count = base, 1
    while (Path(library) / RESTORE_FOLDER / name).exists():
        count += 1
        name = f"{base}-{count}"
    return name


def schema_versions(root):
    """The schema number of each database under `root` that exists, read without changing anything."""
    found = {}
    for key, relative in DATABASES.items():
        path = Path(root) / relative
        if path.is_file():
            try:
                db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
                try:
                    found[key] = db.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    db.close()
            except sqlite3.Error:
                found[key] = None
    return found


def library_counts(root):
    """How much is in the library: characters, voices, references, takes and scenes. A database that does not exist yet
    counts as zero."""
    wanted = {"cast": (("characters", "characters"), ("identities", "identities"), ("references", "reference_versions"),
                       ("takes", "takes")),
              "scenes": (("scenes", "scenes"),)}
    counts = {}
    for key, tables in wanted.items():
        path = Path(root) / DATABASES[key]
        db = None
        if path.is_file():
            try:
                db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            except sqlite3.Error:
                db = None
        for label, table in tables:
            try:
                counts[label] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if db else 0
            except sqlite3.Error:
                counts[label] = None
        if db:
            db.close()
    return counts


def _open_stores(root):
    """Open the unpacked library the way the service does, which moves a database from an older schema forward and fails
    plainly when it cannot."""
    from cast_store import CastStore
    from evaluation_store import EvaluationStore
    from scene_store import SceneStore
    try:
        cast = CastStore(root)
        cast.list_cast()
        SceneStore(cast, {}, root=root)
        EvaluationStore(root / DATABASES["evaluations"], lab_root=root)
    except Exception as exc:
        raise BackupError(f"This build cannot open the restored library: {type(exc).__name__}: {exc}") from exc


def _refuse_newer_schema(manifest):
    from cast_store import CAST_SCHEMA_VERSION
    from evaluation_store import SCHEMA_VERSION as EVALUATION_SCHEMA
    from scene_store import SCHEMA_VERSION as SCENE_SCHEMA
    supported = {"cast": CAST_SCHEMA_VERSION, "scenes": SCENE_SCHEMA, "evaluations": EVALUATION_SCHEMA}
    for key, record in manifest["databases"].items():
        if record["schema_version"] > supported[key]:
            raise BackupError(f"This backup holds a {DATABASE_LABEL[key]} database at schema {record['schema_version']}, "
                              f"newer than the schema {supported[key]} this build reads. Update the app, then restore it.")


def _media_warnings(root, manifest):
    """Compare the media the restored Cast database refers to with the files that came with it. Files the library was
    already missing when the backup was made are a warning; more missing files than that mean the backup lost some."""
    if not manifest.get("media_checked"):
        return []
    missing = [path for path in _referenced_media(root / DATABASES["cast"]) or [] if not (root / path).is_file()]
    if len(missing) > manifest.get("missing_media_total", 0):
        known = set(manifest.get("missing_media", []))
        lost = next((path for path in missing if path not in known), missing[0])
        raise BackupError("Media files that the restored Cast database refers to are missing from the backup, for example "
                          f"{lost}. Nothing was restored.")
    if missing:
        return [f"{len(missing)} media files that the Cast database refers to were already missing from the library when "
                f"the backup was made, for example {missing[0]}."]
    return []


def restore(zip_path, new_root, *, progress=None):
    """Unpack a backup into `new_root`, which must not exist or must be empty, and check it before it takes that name.
    Nothing in the library in use is touched. Returns {"state_root", "files", "bytes", "counts", "schemas", "warnings"};
    raises BackupError, leaving nothing behind, when the backup does not check out."""
    zip_path, target = Path(zip_path), Path(new_root).resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError(f"{target} already exists and is not empty. A restore never writes into an existing folder; "
                          "choose a new folder.")
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"The file cannot be read as a backup: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=target.name + ".restoring-", dir=target.parent))
    try:
        with archive:
            manifest = _read_manifest(archive)
            _refuse_newer_schema(manifest)
            total = sum(entry["size"] for entry in manifest["files"])
            free = shutil.disk_usage(target.parent).free
            if free < total + MARGIN_BYTES:
                raise BackupError(f"There is not enough free space to restore: it needs about "
                                  f"{format_bytes(total + MARGIN_BYTES)} and {format_bytes(free)} is free.")

            def sink(relative):
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    return destination.open("xb")
                except FileExistsError as exc:
                    raise BackupError(f"Two files in the backup have the same name here: {relative}.") from exc

            problems = _read_members(archive, manifest, sink=sink,
                                     report=_Progress(len(manifest["files"]), total, progress))
        for key, record in manifest["databases"].items():
            problems += _database_problems(staging / record["path"], DATABASE_LABEL[key], record)
        if problems:
            raise BackupError("The backup does not check out, so nothing was restored: " + " ".join(_limit(problems)))
        _open_stores(staging)
        warnings = _media_warnings(staging, manifest)
        counts = library_counts(staging)
        if target.exists():
            target.rmdir()
        _move_into_place(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"state_root": str(target), "files": len(manifest["files"]), "bytes": total, "counts": counts,
            "schemas": schema_versions(target), "warnings": warnings}


def _move_into_place(staging, target):
    """Rename the checked folder to its final name. A virus scanner or indexer can hold a file for a moment on Windows,
    so a refusal is retried a few times before it is reported."""
    for attempt in range(5):
        try:
            os.replace(staging, target)
            return
        except OSError as exc:
            if attempt == 4:
                raise BackupError(f"The checked copy could not be moved to {target}: {exc}. Nothing was restored.") from exc
            time.sleep(0.4)


def configured_state_root(app_root):
    """The library the desktop app opens: the state_root in desktop.local.json, read against the app folder as the
    launcher does, else the app folder itself."""
    path = Path(app_root) / "desktop.local.json"
    try:
        settings = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        settings = {}
    value = settings.get("state_root") if isinstance(settings, dict) else None
    return (Path(app_root) / value).resolve() if isinstance(value, str) and value else Path(app_root).resolve()


def use_library(app_root, state_root):
    """Point the desktop app at `state_root` the next time it starts, by writing desktop.local.json beside it. Returns the
    folder it used before. A settings file that is not valid JSON is never overwritten."""
    folder = Path(state_root).resolve()
    if not (folder / DATABASES["cast"]).is_file():
        raise BackupError(f"{folder} is not a library: it has no Cast database.")
    path = Path(app_root) / "desktop.local.json"
    settings = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError(f"{path.name} cannot be read ({exc}); fix or delete it first.") from exc
        if not isinstance(settings, dict):
            raise BackupError(f"{path.name} is not a settings object; fix or delete it first.")
    previous = settings.get("state_root")
    settings["state_root"] = str(folder)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((json.dumps(settings, indent=2) + "\n").encode("utf-8"))
    os.replace(temporary, path)
    return {"state_root": str(folder), "previous": previous, "settings_file": str(path)}


class Task:
    """One long job at a time (a backup, a check or a restore), run on a thread, with progress a page can poll."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"status": "idle"}

    def start(self, kind, label, work):
        """Run work(progress) in the background; it returns a result dict or raises BackupError. Refuses while another
        job is running."""
        with self._lock:
            if self._state.get("status") == "running":
                raise BackupBusy(f"A {self._state['kind']} is already running. Wait for it to finish.")
            self._state = {"status": "running", "kind": kind, "label": label, "started_at": _stamp(),
                           "files_done": 0, "files_total": 0, "bytes_done": 0, "bytes_total": 0}
            threading.Thread(target=self._run, args=(work,), name=f"library-{kind}", daemon=True).start()
            return dict(self._state)

    def _progress(self, files_done, files_total, bytes_done, bytes_total):
        with self._lock:
            self._state.update(files_done=files_done, files_total=files_total, bytes_done=bytes_done, bytes_total=bytes_total)

    def _run(self, work):
        try:
            outcome = {"status": "done", "result": work(self._progress)}
        except BackupError as exc:
            outcome = {"status": "failed", "error": str(exc)}
        except Exception as exc:
            outcome = {"status": "failed", "error": f"It stopped unexpectedly: {type(exc).__name__}: {exc}"}
        with self._lock:
            self._state.update(outcome, finished_at=_stamp())

    def status(self):
        with self._lock:
            return dict(self._state)
