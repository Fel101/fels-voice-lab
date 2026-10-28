"""What a person needs to see, or paste into a bug report, about this installation.

build() gathers it from this machine without loading a model: the version, Python and Windows, the device, FFmpeg and what
it can write, free disk, the schema number and size of the library, which models are usable, the service log and the
backups. diagnostics_text() turns that into plain text for the clipboard. The text holds no script, transcript, name of a
character or voice, or recording, and the home folder reads as ~. storage() measures the folders the panel lists and what
the cleanup would remove.
"""

import os
import platform
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import housekeeping
import library_backup

LOW_DISK_BYTES = 5 * 1024 ** 3
ENGINE_STATES = ("usable", "not_checked", "check_outdated", "check_failed", "not_installed", "conversion_task")
STATE_WORDS = {"usable": "usable", "not_checked": "installed, not checked", "check_outdated": "check out of date",
               "check_failed": "check failed", "not_installed": "not installed", "conversion_task": "conversion task"}
COUNT_LABELS = (("characters", "character", "characters"), ("identities", "voice identity", "voice identities"),
                ("references", "reference", "references"), ("takes", "take", "takes"), ("scenes", "scene", "scenes"))


def redact(text):
    """The home folder becomes ~, in either slash style and (on Windows) any letter case, so a report can be shared. A longer
    name that merely starts like it (C:\\Users\\sam2 beside C:\\Users\\sam) is not the home folder and stays as it is."""
    text = str(text)
    home = str(Path.home())
    flags = re.IGNORECASE if os.name == "nt" else 0
    for form in sorted({home, home.replace("\\", "/")}, key=len, reverse=True):
        text = re.sub(re.escape(form) + r"(?![\w\-]|\.\w)", lambda _match: "~", text, flags=flags)
    return text


def disk(path):
    """Free and total bytes of the drive holding `path` (or its nearest folder that exists); None when unreadable."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return {"free_bytes": usage.free, "total_bytes": usage.total, "low": usage.free < LOW_DISK_BYTES}


def engines_summary(payload):
    """How many models are in each state, and one row per integrated model, from engines.engine_status()."""
    rows = [{"id": row["id"], "name": row["name"], "state": row["state"]} for row in payload.get("engines", [])
            if row["state"] != "not_integrated"]
    counts = {state: 0 for state in ENGINE_STATES}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1  # a state added later still shows, under its own name
    return {"device": payload.get("device"), "counts": counts, "rows": rows}


def log_info(path):
    if not path:
        return None
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {"file": str(path), "folder": str(path.parent), "bytes": size}


def build(*, app, version, state_root, out_dir, device, ffmpeg, engine_status, log_path, free_ram):
    """The report as a dict the page renders. Paths are real here; diagnostics_text() redacts them."""
    root = Path(state_root)
    backups = library_backup.list_backups(root / library_backup.BACKUP_FOLDER)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app": {"name": app, "version": version},
        "system": {"python": platform.python_version(), "platform": platform.platform(), "device": device,
                   "free_ram_gb": round(free_ram, 1)},
        "library": {"path": str(root), "schemas": library_backup.schema_versions(root),
                    "counts": library_backup.library_counts(root)},
        "ffmpeg": ffmpeg,
        "disk": {"library": disk(root), "install": disk(out_dir)},
        "engines": engines_summary(engine_status),
        "log": log_info(log_path),
        "backups": {"folder": str(root / library_backup.BACKUP_FOLDER), "count": len(backups),
                    "latest": backups[0] if backups else None},
    }


def _gb(count):
    return f"{count / 1024 ** 3:.1f} GB"


def diagnostics_text(report):
    """Plain text for a bug report: facts about the installation and none of the work in it."""
    app, system, library = report["app"], report["system"], report["library"]
    schemas = ", ".join(f"{key} {'unreadable' if value is None else value}"
                        for key, value in library["schemas"].items()) or "no databases yet"
    counts = library["counts"]
    ffmpeg = report["ffmpeg"]
    if ffmpeg.get("found"):
        formats = ", ".join(f"{label} {'yes' if ffmpeg['formats'].get(key) else 'no'}"
                            for key, label in (("wav", "WAV"), ("flac", "FLAC"), ("ogg", "Ogg"), ("mp3", "MP3"), ("m4a", "AAC")))
        ffmpeg_line = (f"FFmpeg {ffmpeg.get('version') or '(version unknown)'} found; "
                       f"{'ffprobe found' if ffmpeg.get('ready') else 'ffprobe missing'}; can write: {formats}")
    else:
        ffmpeg_line = "FFmpeg not found"
    drives = []
    for label, key in (("library drive", "library"), ("install drive", "install")):
        row = report["disk"].get(key)
        drives.append(f"{label} {_gb(row['free_bytes'])} free of {_gb(row['total_bytes'])}" if row else f"{label} unreadable")
    engines = report["engines"]
    states = ", ".join(f"{count} {STATE_WORDS.get(state, state)}" for state, count in engines["counts"].items() if count)
    lines = [
        f"{app['name']} {app['version']} diagnostics, {report['generated_at']}",
        f"System: Python {system['python']}, {system['platform']}, device {system['device']}, "
        f"{system['free_ram_gb']} GB RAM free",
        f"Library: {library['path']}",
        f"  schemas: {schemas}",
        "  contents: " + ", ".join(f"{counts[key]} {one if counts[key] == 1 else many}"
                                for key, one, many in COUNT_LABELS if counts.get(key) is not None),
        ffmpeg_line,
        "Disk: " + "; ".join(drives),
        f"Models (device {engines['device']}): {states or 'none integrated'}",
    ]
    lines += [f"  {row['id']}: {STATE_WORDS.get(row['state'], row['state'])}" for row in engines["rows"]]
    log = report.get("log")
    lines.append(f"Service log: {log['file']} ({log['bytes']} bytes)" if log and log.get("file") else "Service log: none")
    backups = report["backups"]
    newest = backups["latest"]
    lines.append(f"Backups: {backups['count']} in {backups['folder']}"
                 + (f"; newest {newest['name']} ({library_backup.format_bytes(newest['bytes'])})" if newest else ""))
    lines.append("Not included: scripts, transcripts, names of characters or voices, and recordings. "
                 "The home folder is shown as ~.")
    return redact("\n".join(lines))


def storage(state_root, out_dir):
    """Folder sizes for the panel, and what the cleanup would remove now. It walks the library and the run folders and never
    out/drama3, which holds the Drama model."""
    root, out = Path(state_root), Path(out_dir)
    sections = [{"key": row["key"], "label": row["label"], "bytes": row["bytes"], "files": row["files"],
                 "optional": row["optional"]} for row in library_backup.plan(root, library_backup.OPTIONAL)["sections"]]
    other = []
    for key, label, folder in (("backups", "Backups", root / library_backup.BACKUP_FOLDER),
                               ("conversions", "Voice conversions (not in a backup)", out / "conversions"),
                               ("logs", "Service logs", out / "logs"), ("workers", "Worker scratch folders", out / "workers"),
                               ("desktop", "Desktop sessions", out / "desktop")):
        other.append({"key": key, "label": label, "bytes": housekeeping.tree_size(folder) if folder.is_dir() else 0})
    return {"library": sections, "other": other, "cleanup": housekeeping.summarize(housekeeping.plan(out))}
