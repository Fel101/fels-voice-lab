"""Studio wrapper over the scene package exporter (tools/scene_export.py).

The browser sends one JSON object. It is validated here, converted to the exporter's
argument namespace, and run in process with the same checks as the command line. Packages
go only under the state root's out/exports folder, and an existing package is never
overwritten.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import re
import sqlite3
import subprocess
import threading
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import ffmpeg_tools

SCENE_ID = re.compile(r"[a-f0-9]{32}")
EXPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,89}")
FORMATS = ("wav", "flac", "ogg", "mp3", "m4a")
MAX_LISTED = 50
_export_lock = threading.Lock()
# The same failure set the command-line exporter reports as a clean error.
_FAILURES = (ValueError, KeyError, TypeError, OSError, sqlite3.Error, subprocess.SubprocessError)


class ExportBusy(RuntimeError):
    """Another package is still being written; the client should try again later."""


def _exporter():
    # Imported lazily so a missing scientific dependency cannot stop the lab server from starting.
    from tools import scene_export
    return scene_export


def _number(req, key, kind, default=None):
    value = req.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number.")
    if kind is int:
        if not float(value).is_integer():
            raise ValueError(f"{key} must be a whole number.")
        return int(value)
    return float(value)


def _default_name():
    return "panel-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]


def _build_args(state_root, req, *, dry_run):
    scene_id = req.get("scene_id")
    if not isinstance(scene_id, str) or not SCENE_ID.fullmatch(scene_id):
        raise ValueError("Choose a saved scene to export.")
    formats = req.get("formats", ["wav"])
    if not isinstance(formats, list) or not formats or not all(isinstance(item, str) for item in formats):
        raise ValueError("Choose at least one deliverable format.")
    if len(set(formats)) != len(formats):
        raise ValueError("Each format may be chosen only once.")
    unknown = sorted(set(formats) - set(FORMATS))
    if unknown:
        raise ValueError(f"Unsupported format(s): {', '.join(unknown)}.")
    template = req.get("filename_template", "{line_id}")
    if not isinstance(template, str) or not template.strip() or len(template) > 120:
        raise ValueError("Filename template must be 1 to 120 characters.")
    name = req.get("name")
    if name in (None, ""):
        name = _default_name()
    elif not isinstance(name, str) or not EXPORT_NAME.fullmatch(name):
        raise ValueError("Package folder names may use letters, numbers, dots, dashes and underscores (90 characters at most).")
    loudness = _number(req, "loudness_lufs", float)
    return Namespace(
        command="export", project_root=str(state_root), scene_id=scene_id, character_id=None,
        formats=formats, name=name, filename_template=template, dry_run=dry_run,
        ffmpeg=None, ffprobe=None,
        output_rate=_number(req, "output_rate", int),
        trim_start_seconds=_number(req, "trim_start_seconds", float, 0.0),
        trim_end_seconds=_number(req, "trim_end_seconds", float, 0.0),
        fade_in_ms=_number(req, "fade_in_ms", int, 0),
        fade_out_ms=_number(req, "fade_out_ms", int, 0),
        loudness_lufs=loudness,
        # The exporter applies -1 dBTP when a loudness target is set without a true-peak value.
        loudness_true_peak=_number(req, "loudness_true_peak", float) if loudness is not None else None,
        limiter_dbfs=_number(req, "limiter_dbfs", float),
    )


def _export_dir(state_root, name):
    if not isinstance(name, str) or not EXPORT_NAME.fullmatch(name):
        raise ValueError("Unknown package folder name.")
    exports = (Path(state_root) / "out" / "exports").resolve()
    directory = (exports / name).resolve()
    if directory.parent != exports or not directory.is_dir():
        raise ValueError(f"Package folder not found: {name}")
    return directory


def _summary(directory):
    row = {"name": directory.name, "path": f"out/exports/{directory.name}"}
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        row["status"] = "incomplete" if (directory / "INCOMPLETE.txt").is_file() else "unverified"
        return row
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        row.update({
            "status": "complete",
            "created_at": manifest.get("created_at"),
            "scene_id": manifest["scene"]["scene_id"],
            "scene_name": manifest["scene"]["name"],
            "scene_revision": manifest["scene"]["scene_revision"],
            "formats": manifest.get("formats", []),
            "line_count": len(manifest.get("selection", [])),
            "asset_count": len(manifest.get("assets", [])),
            "sample_rate_hz": (manifest.get("transforms") or {}).get("sample_rate_hz"),
        })
    except (OSError, ValueError, KeyError, TypeError):
        row["status"] = "unreadable"
    return row


def list_exports(state_root):
    exports = Path(state_root) / "out" / "exports"
    if not exports.is_dir():
        return []
    directories = [path for path in exports.iterdir()
                   if path.is_dir() and EXPORT_NAME.fullmatch(path.name)]
    directories.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_summary(path) for path in directories[:MAX_LISTED]]


def ffmpeg_report(refresh=False):
    """What the panel needs to know before anyone presses a button: can packages be written at all, which formats this
    FFmpeg can encode, and in words what is missing."""
    report = ffmpeg_tools.status(refresh=refresh)
    formats = ffmpeg_tools.writable_formats(report)
    ready = bool(report["found"] and report["ffprobe"])
    message = report["message"]
    if ready and not all(formats.values()):
        off = ", ".join(ffmpeg_tools.FORMAT_LABEL[name] for name, usable in formats.items() if not usable)
        message = f"This FFmpeg build cannot write {off}, so that choice is off. Install a full FFmpeg build to turn it on."
    return {"found": report["found"], "ready": ready, "version": report["version"], "formats": formats, "message": message}


def run(state_root, req, *, dry_run=False):
    exporter = _exporter()
    args = _build_args(state_root, req, dry_run=dry_run)
    output = io.StringIO()
    if dry_run:
        try:
            with contextlib.redirect_stdout(output):
                exporter.export_package(args)
        except (exporter.ExportError, *_FAILURES) as exc:
            raise ValueError(str(exc)) from exc
        return {"dry_run": True, "log": output.getvalue()}
    if not _export_lock.acquire(blocking=False):
        raise ExportBusy("Another package is still being written. Wait for it to finish, then try again.")
    try:
        with contextlib.redirect_stdout(output):
            exporter.export_package(args)
    except (exporter.ExportError, *_FAILURES) as exc:
        raise ValueError(str(exc)) from exc
    finally:
        _export_lock.release()
    # The exporter lowercases the requested name and appends a short hash; report the folder it wrote.
    written = exporter.safe_component(args.name)
    return {"ok": True, "export": _summary(_export_dir(state_root, written)), "log": output.getvalue()}


def verify(state_root, name):
    exporter = _exporter()
    directory = _export_dir(state_root, name)
    args = Namespace(command="verify", project_root=str(state_root), export_dir=str(directory))
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exporter.verify_export(args)
    except (exporter.ExportError, *_FAILURES) as exc:
        raise ValueError(str(exc)) from exc
    return {"ok": True, "name": name, "log": output.getvalue()}
