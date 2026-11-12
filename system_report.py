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
import sys
from datetime import datetime, timezone
from pathlib import Path

import housekeeping
import library_backup

LOW_DISK_BYTES = 5 * 1024 ** 3
# The Lab's own Python, which setup.bat fetches when the computer has none (tools/get_python.ps1).
LAB_PYTHON = Path(__file__).resolve().parent / "python"
ENGINE_STATES = ("usable", "not_checked", "check_outdated", "check_failed", "not_installed", "conversion_task")
STATE_WORDS = {"usable": "usable", "not_checked": "installed, not tested", "check_outdated": "test out of date",
               "check_failed": "test failed", "not_installed": "not installed", "conversion_task": "conversion task"}
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
    """How many models are in each state, one row per integrated model with where its last test ran and how fast, and the
    speed table, from engines.engine_status()."""
    rows = []
    for row in payload.get("engines", []):
        if row["state"] == "not_integrated":
            continue
        check = row.get("check") or {}
        rows.append({"id": row["id"], "name": row["name"], "state": row["state"],
                     **{key: check[key] for key in ("ran_on", "real_time_factor") if check.get(key) is not None}})
    counts = {state: 0 for state in ENGINE_STATES}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1  # a state added later still shows, under its own name
    speed = [{key: row[key] for key in ("engine", "device", "clips", "median_real_time_factor")}
             for row in payload.get("speed", []) if row.get("median_real_time_factor") is not None]
    return {"device": payload.get("device"), "counts": counts, "rows": rows, "speed": speed}


def python_source(base=None):
    """Where the Python the Lab runs on lives: "the Lab's own" for the python folder setup.bat fetched, else its folder.
    The service runs in .venv, so this is the Python .venv was built from, which every model's environment is built from too."""
    folder = Path(base or getattr(sys, "_base_executable", sys.executable)).resolve().parent
    try:
        own = folder == LAB_PYTHON.resolve()
    except OSError:
        own = False
    return "the Lab's own" if own else str(folder)


def hardware_facts():
    """The processor and graphics cards, as Windows names them (hardware.py): what a hardware report needs."""
    import hardware
    return {"processor": hardware.processor_name(), "graphics_cards": hardware.graphics_cards()}


def log_info(path):
    if not path:
        return None
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {"file": str(path), "folder": str(path.parent), "bytes": size}


def build(*, app, version, state_root, out_dir, device, ffmpeg, engine_status, log_path, free_ram, total_ram=None,
          hardware=None):
    """The report as a dict the page renders. Paths are real here; diagnostics_text() redacts them. `hardware` is
    hardware_facts() or a stand-in for it."""
    root = Path(state_root)
    backups = library_backup.list_backups(root / library_backup.BACKUP_FOLDER)
    facts = hardware_facts() if hardware is None else hardware
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app": {"name": app, "version": version},
        "system": {"python": platform.python_version(), "python_source": python_source(), "platform": platform.platform(),
                   "device": device,
                   "free_ram_gb": round(free_ram, 1), "total_ram_gb": None if total_ram is None else round(total_ram, 1),
                   "processor": facts.get("processor"), "graphics_cards": facts.get("graphics_cards") or []},
        "library": {"path": str(root), "schemas": library_backup.schema_versions(root),
                    "counts": library_backup.library_counts(root)},
        "ffmpeg": ffmpeg,
        "disk": {"library": disk(root), "install": disk(out_dir)},
        "engines": engines_summary(engine_status),
        "log": log_info(log_path),
        "backups": {"folder": str(root / library_backup.BACKUP_FOLDER), "count": len(backups),
                    "latest": backups[0] if backups else None},
    }


def python_words(system):
    source = system.get("python_source")
    return f" ({source})" if source == "the Lab's own" else f" (from {source})" if source else ""


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
    total = f" of {system['total_ram_gb']} GB" if system.get("total_ram_gb") else ""
    lines = [
        f"{app['name']} {app['version']} diagnostics, {report['generated_at']}",
        f"System: Python {system['python']}{python_words(system)}, {system['platform']}, device {system['device']}, "
        f"{system['free_ram_gb']} GB RAM free{total}",
    ]
    if system.get("processor"):
        lines.append(f"Processor: {system['processor']}")
    if "graphics_cards" in system:
        cards = [card["name"] + (f" (driver {card['driver']})" if card.get("driver") else "") for card in system["graphics_cards"]]
        lines.append("Graphics: " + ("; ".join(cards) if cards else "none found"))
    lines += [
        f"Library: {library['path']}",
        f"  schemas: {schemas}",
        "  contents: " + ", ".join(f"{counts[key]} {one if counts[key] == 1 else many}"
                                for key, one, many in COUNT_LABELS if counts.get(key) is not None),
        ffmpeg_line,
        "Disk: " + "; ".join(drives),
        f"Models (device {engines['device']}): {states or 'none integrated'}",
    ]
    for row in engines["rows"]:
        tested = ""
        if row.get("ran_on"):
            speed = f", {row['real_time_factor']:.2f} s per second of audio" if row.get("real_time_factor") is not None else ""
            tested = f" (tested on {row['ran_on']}{speed})"
        lines.append(f"  {row['id']}: {STATE_WORDS.get(row['state'], row['state'])}{tested}")
    if engines.get("speed"):
        lines.append("Speed of recent clips: " + "; ".join(
            f"{row['engine']} on {row['device']}, {row['median_real_time_factor']:.2f} s per second of audio over "
            f"{row['clips']} clip{'' if row['clips'] == 1 else 's'}" for row in engines["speed"]))
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
