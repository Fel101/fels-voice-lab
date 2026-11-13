"""Fel's Voice Lab's local service: the studio pages, the stores behind them and the HTTP API (docs/API.md).

    .venv\\Scripts\\python serve.py          then open http://127.0.0.1:8765

Local only (127.0.0.1). Models load the first time you use them and are unloaded after
--idle-minutes without a request, so the GPU is not held while other programs need it.
The /api/speak endpoint keeps a simple request and WAV response shape for external callers.
"""

import argparse
import base64
import contextlib
from datetime import datetime
import functools
import hashlib
import json
import os
import re
import select
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from common import DEVICES, LAB, OUT, free_ram_gb, pick_device, ram_report, require_ram
from ram_needs import MIN_FREE_RAM_GB

import soundfile as sf

import api_spec
import engine_adapters
import job_queue
from routes import Router
import route_groups
import library_backup
import synthetic_label
import updates
from app_version import APP_NAME, VERSION
from housekeeping import run_startup_sweep
from request_log import NullRequestLog, RequestLog, new_request_id
from runtime_folders import migrate_program_folders

from worker_pool import EnginePool, WorkerError
from engine_checks import EngineCheckStore
from engine_devices import EngineDeviceStore
from engine_install import EngineInstaller
from cast_store import (CastStore, DIRECTION_ENGINES, MAX_UPLOAD_BYTES,
                        PRESET_VOICE_ENGINES, decode_audio, generation_identity_snapshot)
from scene_store import SceneBatchRunner, SceneStore, SUPPORTED_LANGUAGES, csv_template, json_template
from script_import import parse_script
import export_panel
import speaker_similarity
import transcript_suggest
from evaluation_store import EvaluationStore
from evaluation_cast import import_cast_takes
from evaluation_suites import list_suites
from ui_preferences import UiPreferences
from reference_catalog import list_references, list_voice_library, resolve_reference_audio, save_designed_reference
from kokoro_support import discover_kokoro_voices
from performance_settings import DEFAULTS, effective_settings, performance_controls
from language_matrix import unscreened_languages

LOCAL_HOSTS = ("127.0.0.1", "localhost")
# The Lab's pages. Another website may open them in a tab, and may make no other request (Handler._local_only).
PAGE_PATHS = ("/", "/index.html", "/compare", "/compare.html")
MAX_REQUEST_BYTES = 64 * 1024
MAX_SCENE_REQUEST_BYTES = 2 * 1024 * 1024
# A body that is refused for its size is still read through, up to this size and this long, before the refusal goes out
# (Handler._discard_body).
MAX_DISCARD_BYTES = 512 * 1024 * 1024
DISCARD_SECONDS = 20
VOICE_ASSET_NAME_RE = re.compile(
    r"(?:(?:[A-Za-z0-9][A-Za-z0-9_-]{0,31})/){0,2}[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z"
)

def _page(name):
    path = LAB / name
    return path.read_text(encoding="utf-8").replace("__APP_VERSION__", VERSION) if path.exists() else f"{name} missing"


PAGE = _page("serve.html")
# Scripts that serve.html loads from this folder. Only these names are served, never arbitrary paths.
PAGE_SCRIPTS = frozenset({"voice_lab_queue.js", "voice_lab_studio.js", "voice_lab_cast.js", "voice_lab_take_edit.js", "voice_lab_scenes.js",
                          "voice_lab_cast_bindings.js", "voice_lab_generate.js", "voice_lab_system.js", "voice_lab_app.js",
                          "voice_lab_drama.js"})
# The folder that holds desktop.local.json, which tells the desktop app which library to open. The tests point it elsewhere.
APP_ROOT = LAB
COMPARE_PAGE = _page("compare.html")
AUDIO_EXTENSIONS = {".wav", ".flac", ".ogg"}
# Voice conversion's two inputs: what each is called, and how long it may be.
CONVERSION_ROLES = {"source": "Source performance", "target": "Target voice reference"}
CONVERSION_SECONDS = {"source": 60.0, "target": 10.0}
CONVERSION_PERMISSION = ("To use a recording as the target voice, confirm that you own it or have permission to clone the "
                         "voice, and record its origin and allowed use.")


def _conversion_uploads():
    """Where Voice conversion keeps the files chosen from this computer: out/conversions/uploads/source and .../target."""
    return OUT / "conversions" / "uploads"


def _write_whole(path, data):
    """Write a file whole, through a temporary file beside it, so that a reader never sees half of it."""
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def save_conversion_upload(body, metadata):
    """Keep a file chosen on the Voice conversion tab: read it as Cast imports do (WAV, FLAC, OGG and MP3, M4A through
    FFmpeg), check its length for its role, and save it as a mono WAV named by the SHA-256 of the bytes sent, so the same file
    sent twice lands in the same place. Beside it goes a JSON with its original name, its role, the time and, for a target
    voice, the permission, origin and allowed use it was given, which the lists and the conversion manifest read. A target
    needs all three. The name the browser sends sets the format and the label only, never a path. Returns what the lists
    show for it, with its path."""
    role = metadata.get("role")
    if role not in CONVERSION_ROLES:
        raise ValueError('Say whether the file is the source performance or the target voice: "role" is "source" or "target".')
    name = metadata.get("filename")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Send the file's name as filename: its extension says how to read it.")
    original = re.split(r"[\\/]", name.strip())[-1][:255]
    terms = {}
    if role == "target":
        origin, allowed = metadata.get("origin"), metadata.get("allowed_use")
        if metadata.get("permission") is not True or not all(isinstance(text, str) and text.strip() for text in (origin, allowed)):
            raise ValueError(CONVERSION_PERMISSION)
        if len(origin) > 1000 or len(allowed) > 1000:
            raise ValueError("Keep the origin and the allowed use under 1,000 characters each.")
        terms = {"permission": True, "origin": origin.strip(), "allowed_use": allowed.strip()}
    what = CONVERSION_ROLES[role]
    audio, rate, seconds, _suffix = decode_audio(
        body, original, what=what, max_seconds=CONVERSION_SECONDS[role],
        length=f"A {what.lower()} must be {{minimum:g}} to {{maximum:g}} seconds long. Trim it, or choose a shorter one.")
    import io
    import numpy as np
    wav = io.BytesIO()
    sf.write(wav, np.clip(audio.mean(axis=1), -1.0, 1.0), rate, format="WAV", subtype="PCM_16")
    digest = hashlib.sha256(body).hexdigest()
    folder = _conversion_uploads() / role
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.wav"
    _write_whole(path, wav.getvalue())
    record = {"schema": 1, "role": role, "original_name": original, "original_sha256": digest,
              "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "duration_seconds": round(float(seconds), 3), "sample_rate_hz": int(rate), **terms}
    _write_whole(path.with_suffix(".json"), (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return {"path": str(path.resolve()), **{key: value for key, value in record.items() if key != "schema"}}


def _upload_record(path):
    """The JSON beside a file kept by save_conversion_upload: {} when it cannot be read, and None for any other file."""
    path = Path(path)
    if _conversion_uploads().resolve() not in path.parents:
        return None
    try:
        record = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _upload_row(path, target):
    """What a conversion list shows of a file chosen from this computer: its original name, and a target's terms. None for a
    file that is not one, and False for one that the list must not offer: an upload made for the other role, or a target
    whose permission, origin and allowed use are not all recorded."""
    record = _upload_record(path)
    if record is None:
        return None
    if Path(path).parent.name != ("target" if target else "source"):
        return False
    original = str(record.get("original_name") or "")
    row = {"name": original or Path(path).name, "original_name": original, "uploaded_at": str(record.get("uploaded_at", ""))}
    if target:
        terms = [str(record.get(key) or "").strip() for key in ("origin", "allowed_use")]
        if record.get("permission") is not True or not all(terms):
            return False
        row.update(origin=terms[0], allowed_use=terms[1])
    return row


def _conversion_roots(target=False, cast=None):
    paths = [LAB / "reference", LAB / "data" / "voices", LAB / "data" / "cast" / "media" / "references"]
    if not target:
        paths.extend((LAB / "data" / "cast" / "media" / "takes", OUT / "conversions", OUT / "scenes"))
    else:
        paths.append(OUT / "conversions")
    if cast is not None:
        paths.append(cast.data_root / "media" / "references")
        if not target:
            paths.extend((cast.data_root / "media" / "takes", cast.root / "out" / "scenes"))
    return tuple(path.resolve() for path in paths)


def _conversion_audio_path(value, role, *, target=False, cast=None):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{role} audio file is required.")
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{role} audio file does not exist.") from exc
    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError(f"{role} audio must be a local WAV, FLAC, or OGG file.")
    if not any(root in path.parents for root in _conversion_roots(target=target, cast=cast)):
        role_hint = "reference or saved voice" if target else "reference, saved voice, Cast take, scene, or voice conversion"
        raise ValueError(f"{role} audio must be under voice-lab {role_hint} storage.")
    if target and _upload_row(path, True) is False:
        raise ValueError(f"{CONVERSION_PERMISSION} Choose the file under Target voice reference to give them.")
    if cast is not None:
        cast.assert_media_usable(path)
    details = sf.info(path)
    maximum = CONVERSION_SECONDS["target" if target else "source"]
    if details.duration <= 0 or details.duration > maximum:
        raise ValueError(f"{role} audio must be non-empty and no longer than {maximum:g} seconds.")
    return path, details


def _conversion_asset_rows(cast, *, target=False):
    roots = _conversion_roots(target=target, cast=cast)
    metadata_by_path = {}
    for character in cast.list_cast():
        for identity in character["identities"]:
            for reference in identity["references"]:
                if reference.get("retired"):
                    continue
                try:
                    resolved = Path(cast.get_media_path("reference", reference["id"])).resolve()
                except (OSError, KeyError, ValueError):
                    continue
                metadata_by_path[resolved] = {
                    "reference_version_id": reference["id"], "character_name": character["name"],
                    "identity_label": identity["label"], "origin": reference.get("origin", ""),
                    "allowed_use": reference.get("allowed_use", ""),
                }
    if not target:
        for take in cast.list_takes():
            if take.get("retired"):
                continue
            try:
                resolved = Path(cast.get_media_path("take", take["id"])).resolve()
            except (OSError, KeyError, ValueError):
                continue
            metadata_by_path[resolved] = {"take_id": take["id"], "source_provenance": take.get("provenance", {})}
    rows, seen = [], set()
    # Paths are shown relative to the Lab's folder or the Cast store's, compared with resolved paths, so those two are
    # resolved too: a folder spelled otherwise, such as by a short name like RUNNER~1, would match nothing.
    lab, cast_root = LAB.resolve(), Path(cast.root).resolve()
    # The files chosen from this computer come first, so that no number of conversions can push them out of the list.
    for root in ((_conversion_uploads() / ("target" if target else "source")).resolve(), *roots):
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
            if len(rows) >= 500:
                break
            try:
                resolved = candidate.resolve()
                if (not candidate.is_file() or resolved.suffix.lower() not in AUDIO_EXTENSIONS or
                        root not in resolved.parents or resolved in seen or resolved.stat().st_size > 100 * 1024 * 1024):
                    continue
                details = sf.info(resolved)
            except (OSError, RuntimeError, sf.LibsndfileError):
                continue
            if details.duration <= 0:
                continue
            seen.add(resolved)
            upload = _upload_row(resolved, target)
            if upload is False:
                continue
            display_root = lab if lab in resolved.parents else cast_root
            row = {"path": str(resolved), "relative_path": resolved.relative_to(display_root).as_posix(),
                   "name": resolved.name, "source": root.relative_to(display_root).as_posix(),
                   "size_bytes": resolved.stat().st_size, "sample_rate_hz": details.samplerate,
                   "duration_seconds": round(float(details.duration), 3)}
            row.update(metadata_by_path.get(resolved, {}))
            row.update(upload or {})
            rows.append(row)
    return rows


def performance_schema(key, entry):
    """One engine's performance controls, the languages its picker offers, and the ones the language screen did not clear."""
    schema = performance_controls(key, entry.get("capabilities", {}))
    schema["languages"] = sorted(SUPPORTED_LANGUAGES.get(key, {"English"}))
    return schema


def unscreened_by_engine(engine_ids):
    """Languages the language screen did not clear, per engine, for every engine the app offers (not only those with controls)."""
    marks = {key: unscreened_languages(key) for key in engine_ids}
    return {key: labels for key, labels in marks.items() if labels}


def byte_range(header, size):
    """The part of a body of size bytes that a Range header asks for, as (first, last) with both included. None means send
    the whole body: no header, one this service does not read, or several ranges at once, which a server may answer whole.
    False means no byte asked for is in the body."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not match or match.groups() == ("", ""):
        return None
    first, last = match.groups()
    if not first:  # bytes=-N asks for the last N bytes
        count = int(last)
        return (max(size - count, 0), size - 1) if count and size else False
    first = int(first)
    if last and int(last) < first:
        return None
    if first >= size:
        return False
    return first, (min(int(last), size - 1) if last else size - 1)


def answer_unexpected_errors(method):
    """Give a request whose route raised something it does not handle a 500 that names the error. Without one the connection
    just closes, and the page can only say "Failed to fetch". An answer that had already started is left as it is; either
    way handle_one_request writes the failure to the service log."""
    @functools.wraps(method)
    def answer(self):
        try:
            return method(self)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            raise
        except Exception as exc:
            if self._status:
                raise
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
    return answer


# What the queue (GET /api/queue) calls the work a route asks of the model worker.
QUEUE_LABELS = {"/api/speak": "Clip", "/api/stream": "Live clip", "/api/performance": "Take", "/api/load": "Load",
                "/api/unload": "Unload", "/api/engines/{engine_id}/check": "Model test", "/api/convert": "Voice conversion",
                "/api/prepare-reference": "Voice recording", "/api/kokoro/phonemize": "Pronunciation preview"}


class Lab:
    """What the routes share, built once for each handler class: the model pool, the stores, the background task and the
    service log. A route group (route_groups/) reads it as `lab`, and serve.py's own names, such as LAB and OUT, as
    lab.serve.NAME, so that a test which points serve.LAB elsewhere moves the routes too."""

    def __init__(self, engines, port, cast_store=None, scene_store=None, batch_runner=None, evaluation_store=None,
                 request_log=None, engine_installer=None, update_checks=None):
        self.serve = sys.modules[__name__]
        self.engines, self.port = engines, port
        self.log = request_log or NullRequestLog()
        self.installer = engine_installer or EngineInstaller(engines)
        # Each card's GPU/CPU switch asks whether the GPU build of its engine can be installed here.
        engines.gpu_install = getattr(self.installer, "gpu_build", None)
        self.cast = cast_store or CastStore()
        self.library_root = Path(getattr(self.cast, "root", LAB))
        self.preferences = UiPreferences(self.library_root)
        # Check for updates answers about this program folder, so its last answer lives in out\update beside update.bat's
        # files.
        self.checks = update_checks or updates.UpdateChecks(OUT / "update")
        self.scenes = scene_store or SceneStore(self.cast, engines.entries)
        self.batches = batch_runner or SceneBatchRunner(self.scenes, f"http://127.0.0.1:{port}")
        # Scenes and Comparisons default to the Cast store's own library, so a handler built on a temporary Cast store
        # cannot reach the real one.
        self.evaluations = evaluation_store or EvaluationStore(lab_root=self.library_root)
        self.backups_folder = self.library_root / library_backup.BACKUP_FOLDER
        self.backup_task = library_backup.Task()
        self._drama, self._drama_lock = [], threading.Lock()

    def logged(self, kind, work, describe):
        """Wrap a backup, check or restore so the service log says how it ended: what was made and how big, or why it failed."""
        def run(progress):
            try:
                result = work(progress)
            except Exception as exc:
                self.log.event(f"{kind} failed: {type(exc).__name__}: {exc}")
                raise
            self.log.event(f"{kind} finished: {describe(result)}")
            return result
        return run

    def drama(self):
        """The Drama studio's jobs (drama3_lab_studio.py), started the first time a route asks for them."""
        with self._drama_lock:
            if not self._drama:
                from drama3_lab_studio import create
                self._drama.append(create(self.engines, self.cast))
            return self._drama[0]

    def close_drama(self):
        if self._drama:
            self._drama[0].close()


class Handler(BaseHTTPRequestHandler):
    """The transport every route answers through: the local-only check, the body readers, _send and _json, and _dispatch,
    which finds a request's route. make_handler gives each service a subclass with its own Lab and router."""

    lab = None  # what the routes share; make_handler gives each subclass its own
    router = None  # the rows of api_spec.ROUTES it answers, each with its function
    _request_id = None
    _status = 0
    _sent_bytes = 0
    _notes = None

    @classmethod
    def close_drama(cls):
        cls.lab.close_drama()

    @classmethod
    def close_installer(cls):
        cls.lab.installer.close()

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        """Time the request and, once it is answered, give the service log its line (see request_log.py)."""
        self.command = None
        self._request_id, self._status, self._sent_bytes, self._notes = None, 0, 0, {}
        started = time.perf_counter()
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # The browser stopped waiting, or closed, before the answer went out. There is nothing left to deliver.
            self.close_connection = True
            self._note(error="the client closed the connection before the answer was sent")
        except BaseException as exc:
            self._note(error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            seconds = time.perf_counter() - started
            if self.command and self.lab.log.wants(self.command, self._status, seconds):
                try:
                    path = urlparse(self.path).path  # the query string can carry text, so it is never logged
                except ValueError:
                    path = "-"
                notes = dict(self._notes)
                trace = notes.pop("traceback", None)
                self.lab.log.request(self._rid(), self.command, path, self._status, seconds,
                                     traceback=trace, bytes=self._sent_bytes or None, **notes)

    def _rid(self):
        if self._request_id is None:
            headers = getattr(self, "headers", None)
            self._request_id = new_request_id(headers.get("X-Request-Id") if headers is not None else None)
        return self._request_id

    def _note(self, **fields):
        """Facts about this request for the service log: the engine, or why it failed."""
        if self._notes is None:
            self._notes = {}
        self._notes.update({key: value for key, value in fields.items() if value not in (None, "")})

    def send_response(self, code, message=None):
        self._status = int(code)
        super().send_response(code, message)

    def end_headers(self):
        self.send_header("X-Request-Id", self._rid())
        # Another website may not show the Lab inside a frame of its own, where it could trick a click, and no answer is
        # read as a type other than the one it is sent as.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _local_only(self):
        """Refuse DNS-rebinding (a foreign Host header) and requests made by another website's page
        (a foreign Origin header). Callers that are not browsers, such as scripts, send no Origin.

        A browser leaves Origin off a plain GET, such as an <audio> or <img> on another website, but every request it
        sends says where it came from in Sec-Fetch-Site: "same-origin" from the Lab's own pages, "none" when the person
        typed the address or opened a bookmark. From anywhere else ("cross-site", or "same-site" for a page on another
        localhost port) the only request allowed is opening one of the Lab's pages in a tab."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            try:
                page = urlparse(self.path).path in PAGE_PATHS
            except ValueError:
                return False
            return (page and self.command == "GET" and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document")
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlparse(origin)
                return (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
                        and (parsed.port or 80) == self.lab.port)
            except ValueError:
                return False
        return True

    def _send(self, code, body, ctype, extra=None):
        # Keep ASCII header values byte-compatible with existing clients. Encode metadata which
        # cannot safely appear in an HTTP header, before buffering the response.
        headers = {}
        encoded = []
        for key, value in (extra or {}).items():
            key.encode("ascii")
            if not re.fullmatch(r"[A-Za-z0-9-]+", key):
                raise ValueError("Invalid response header name.")
            value = str(value)
            if any(ord(char) < 32 or ord(char) >= 127 for char in value):
                value = quote(value, safe="")
                encoded.append(key)
            headers[key] = value
        if encoded:
            headers["X-Metadata-Encoding"] = "percent-encoded UTF-8: " + ", ".join(encoded)
        ctype.encode("ascii")
        if "\r" in ctype or "\n" in ctype:
            raise ValueError("Invalid response content type.")
        if code == 200 and self.command == "GET" and ctype.startswith("audio/"):
            # An <audio> player seeks by asking for part of the file. Chromium lets it seek only when the answer says
            # parts can be asked for, so every recording says so and a request for a part gets that part.
            headers["Accept-Ranges"] = "bytes"
            # If-Range asks for the part only if the file is unchanged, which this service cannot tell: send it whole.
            wanted = None if self.headers.get("If-Range") else byte_range(self.headers.get("Range"), len(body))
            if wanted is False:
                error = json.dumps({"error": "The range asked for is not in the file."}).encode("utf-8")
                return self._send(416, error, "application/json", {"Content-Range": f"bytes */{len(body)}"})
            if wanted:
                first, last = wanted
                headers["Content-Range"] = f"bytes {first}-{last}/{len(body)}"
                code, body = 206, body[first:last + 1]
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self._sent_bytes += len(body)

    def _json(self, code, obj):
        if code >= 400 and isinstance(obj, dict):
            self._note(error=obj.get("error"))
        if code >= 500 and sys.exc_info()[0] is not None:
            self._note(traceback=traceback.format_exc())
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _declared_length(self):
        """The body length the request declares, or 0 when it declares none or something unusable."""
        try:
            return max(int(self.headers.get("Content-Length") or "0"), 0)
        except ValueError:
            return 0

    def _discard_body(self, length, limit=None):
        """Read through a body that is being refused. Closing the connection with it unread resets the connection, and a
        client that is still sending then gets the reset and never reads the refusal. A body too big to read through
        (`limit`, MAX_DISCARD_BYTES unless a smaller one is given), or sent too slowly, is left unread."""
        if length > (MAX_DISCARD_BYTES if limit is None else limit):
            return
        patience = self.connection.gettimeout()
        deadline = time.monotonic() + DISCARD_SECONDS
        try:
            while length > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.rfile.read(min(length, 64 * 1024))
                if not chunk:
                    break
                length -= len(chunk)
        except OSError:  # the client stalled or went away; the refusal is sent anyway
            pass
        finally:
            self.connection.settimeout(patience)

    def _read_body(self, limit):
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0:
            raise ValueError("Negative Content-Length.")
        if length > limit:
            self._discard_body(length)
            raise OverflowError(f"Request body exceeds {limit} bytes.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Request body ended before Content-Length bytes arrived.")
        return body

    def _read_json(self, limit=MAX_REQUEST_BYTES):
        try:
            req = json.loads(self._read_body(limit) or b"{}")
        except OverflowError:
            raise
        except Exception as exc:
            raise ValueError("Bad JSON request body.") from exc
        if not isinstance(req, dict):
            raise ValueError("Request body must be a JSON object.")
        return req

    def _reference_metadata(self):
        """The X-Reference-Metadata header of a recording's upload: base64url JSON with its name, words, origin and terms."""
        encoded = self.headers.get("X-Reference-Metadata", "")
        metadata = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))  # a bad one is a ValueError: 400
        if not isinstance(metadata, dict):
            raise ValueError("Reference metadata must be an object.")
        return metadata

    def _json_body(self, code, obj):
        return self._json(code, obj)

    def _stream_line(self, obj):
        payload = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()
        self._sent_bytes += len(payload)
        if obj.get("type") == "error":  # a stream that fails after its headers went out still answers 200
            self._note(error=obj.get("error"))

    @answer_unexpected_errors
    def do_GET(self):
        if not self._local_only():
            return self._json(403, {"error": "local requests only"})
        return self._dispatch("GET")

    @answer_unexpected_errors
    def do_POST(self):
        if not self._local_only():
            # Another website's page sends its body too. A small one is read through so the refusal is not lost to the reset;
            # anything bigger than a request may be is left unread rather than waited for.
            self._discard_body(self._declared_length(), limit=MAX_REQUEST_BYTES)
            return self._json(403, {"error": "local requests only"})
        return self._dispatch("POST")

    def _dispatch(self, method):
        """Answer with the handler that routes.py finds for the request among the rows of api_spec.ROUTES, or with a 404.

        A POST's JSON body is read here, except for the routes that take a recording's bytes, which read their own. What a
        handler raises and does not answer itself is answered here the same way for every route: a body that is too big is
        413, a thing that does not exist 404, a request the Lab cannot take 400, and a worker's error keeps its own status."""
        parsed = urlparse(self.path)
        path = parsed.path
        found = self.router.match(method, path)
        if found is None:
            if method == "POST":
                # Read the body through, as a refusal does, so the client gets the 404 rather than a reset connection.
                self._discard_body(self._declared_length(), limit=MAX_REQUEST_BYTES)
            return self._json(404, {"error": "not found"})
        route, handler, params = found
        try:
            req = None
            if method == "POST" and route.body_kind != "audio":
                scene_call = path == "/api/scenes" or path.startswith("/api/scenes/")
                req = self._read_json(MAX_SCENE_REQUEST_BYTES if scene_call else MAX_REQUEST_BYTES)
            with self._place_in_line(route, req):
                return handler(self, parsed, req, **params)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            raise
        except Exception as exc:
            if self._status:
                raise  # the answer has begun; answer_unexpected_errors and the request log take it from here
            status = (413 if isinstance(exc, OverflowError) else 404 if isinstance(exc, KeyError)
                      else 400 if isinstance(exc, (ValueError, MemoryError))
                      else exc.status_code if isinstance(exc, WorkerError) else None)
            if status is None:
                raise
            return self._json(status, {"error": str(exc)})

    def _place_in_line(self, route, req):
        """Where this request stands if it needs the model worker (job_queue.py). A JSON body's wait_seconds and
        wait_priority are taken out of it here, for every route: without wait_seconds a busy worker is refused with 409
        at once, as before; with it the request waits in line, as this request's id, until its turn, a cancel, its
        time, or its caller going away."""
        wait = req.pop("wait_seconds", None) if isinstance(req, dict) else None
        priority = req.pop("wait_priority", "normal") if isinstance(req, dict) else "normal"
        wait = job_queue.parse_wait(wait)
        if priority not in job_queue.PRIORITIES:
            raise ValueError("wait_priority must be normal or low.")
        queued = getattr(self.lab.engines, "queued", None)
        if queued is None:
            return contextlib.nullcontext()
        what = QUEUE_LABELS.get(route.path)
        if what and priority == "low":
            what += " for a scene batch"
        return queued(request_id=self._rid(), what=what, wait_seconds=wait, priority=priority,
                      alive=self._caller_present if wait else None)

    def _caller_present(self):
        """False once the caller has closed the connection, so a request waiting in line for it leaves the line."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return not readable or bool(self.connection.recv(1, socket.MSG_PEEK))
        except (OSError, ValueError):
            return False

    def _no_query(self, parsed):
        """True when the request named a query string, which the model routes refuse with a 404 (docs/API.md says so)."""
        return bool(parsed.query)


def make_handler(engines, port, cast_store=None, scene_store=None, batch_runner=None, evaluation_store=None,
                 request_log=None, engine_installer=None, update_checks=None):
    """A handler class for one service: Handler, with the Lab its routes share and the router that answers the rows of
    api_spec.ROUTES."""
    lab = Lab(engines, port, cast_store, scene_store, batch_runner, evaluation_store, request_log, engine_installer,
              update_checks)
    # The parts of the Lab the routes below read by name.
    cast, scenes, batches, evaluations, installer = lab.cast, lab.scenes, lab.batches, lab.evaluations, lab.installer
    drama = lab.drama
    router = Router()  # the rows of api_spec.ROUTES, each answered by a route group's function or one below
    for group in route_groups.GROUPS:
        group.register(router, lab)

    # ------------------------------------------------------------------------------------------------ page helpers

    @router.get("/api/references")
    def reference_list(self, parsed, req):
        self._json(200, {"references": list_references(LAB, cast)})

    @router.get("/api/voice-library")
    def voice_library(self, parsed, req):
        params = parse_qs(parsed.query)
        self._json(200, list_voice_library(LAB, cast, include_retired=(params.get("include_retired") or [""])[0] == "1"))

    @router.get("/api/reference-audio")
    def reference_audio(self, parsed, req):
        try:
            requested = (parse_qs(parsed.query).get("path") or [None])[0]
            target, content_type = resolve_reference_audio(requested, LAB, cast)
            self._send(200, target.read_bytes(), content_type)
        except (OSError, RuntimeError, ValueError):
            self._json(404, {"error": "Reference audio is unavailable or outside this Lab's reference storage."})

    @router.get("/api/conversion/sources")
    def conversion_sources(self, parsed, req):
        self._json(200, {"sources": _conversion_asset_rows(cast, target=False)})

    @router.get("/api/conversion/targets")
    def conversion_targets(self, parsed, req):
        self._json(200, {"targets": _conversion_asset_rows(cast, target=True)})

    @router.post("/api/conversion/uploads")
    def conversion_upload(self, parsed, req):
        body = self._read_body(MAX_UPLOAD_BYTES)
        try:
            metadata = self._reference_metadata()
        except ValueError as exc:
            raise ValueError("Send the file's name and role as base64url JSON in the X-Reference-Metadata header.") from exc
        self._json(201, {"upload": save_conversion_upload(body, metadata)})

    # ------------------------------------------------------------------------------------------------ service and models

    @router.get("/api/queue")
    def queue(self, parsed, req):
        empty = {"running": None, "waiting": [], "max_waiting": job_queue.MAX_WAITING}
        self._json(200, engines.queue() if hasattr(engines, "queue") else empty)

    @router.post("/api/queue/{request_id}/cancel")
    def cancel_queued(self, parsed, req, request_id):
        if not hasattr(engines, "cancel_request"):
            raise KeyError("No request with that id is waiting or running.")
        self._json(200, {"cancelled": engines.cancel_request(request_id)})

    @router.get("/api/health")
    def health(self, parsed, req):
        self._json(200, {**engines.health(), "version": VERSION})

    @router.get("/api/info")
    def info(self, parsed, req):
        self._json(200, {"device": engines.device, "loaded": engines.loaded,
                         "voices": discover_kokoro_voices(), **engine_adapters.info(), "free_ram_gb": round(free_ram_gb(), 1)})

    @router.get("/api/catalog")
    def catalog(self, parsed, req):
        self._json(200, engines.catalog)

    @router.get("/api/openapi.json")
    def openapi(self, parsed, req):
        self._json(200, api_spec.build_openapi())

    @router.get("/api/engines/status")
    def engine_status(self, parsed, req):
        status = engines.engine_status()
        if hasattr(installer, "with_sizes"):
            status = installer.with_sizes(status)
        self._json(200, {**status, "install": installer.snapshot()})

    @router.post("/api/engines/{engine_id}/check")
    def engine_check(self, parsed, req, engine_id):
        self._json(200, {"check": engines.check(engine_id)})

    @router.post("/api/engines/{engine_id}/install")
    def engine_install(self, parsed, req, engine_id):
        dry_run, nvidia = req.get("dry_run", False), req.get("nvidia", False)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be true or false.")
        if not isinstance(nvidia, bool):
            raise ValueError("nvidia must be true or false.")
        if dry_run:
            return self._json(200, {"plan": installer.plan(engine_id, nvidia=nvidia)})
        self._json(200, {"install": installer.start(engine_id, nvidia=nvidia)})

    @router.post("/api/engines/{engine_id}/device")
    def engine_device(self, parsed, req, engine_id):
        self._json(200, {"device": engines.choose_device(engine_id, req.get("device"))})

    @router.get("/api/performance/settings")
    def performance_settings(self, parsed, req):
        schemas = [performance_schema(key, entry) for key, entry in engines.api_entries.items() if key in DEFAULTS]
        self._json(200, {"engines": schemas, "unscreened_languages": unscreened_by_engine(engines.api_entries)})

    @router.post("/api/unload")
    def unload(self, parsed, req):
        if self._no_query(parsed):
            return self._json(404, {"error": "not found"})
        engines.unload()
        self._json(200, {"ok": True})

    @router.post("/api/load")
    def load(self, parsed, req):
        if self._no_query(parsed):
            return self._json(404, {"error": "not found"})
        self._json(200, engines.load(req.get("engine", engine_adapters.DEFAULT_ENGINE), profile=req.get("profile")))

    @router.post("/api/prepare-reference")
    def prepare_reference(self, parsed, req):
        if self._no_query(parsed):
            return self._json(404, {"error": "not found"})
        self._json(200, engines.prepare_reference(req.get("engine"), req.get("path"),
                                                  exaggeration=req.get("exaggeration", 0.5), profile=req.get("profile"),
                                                  reference_text=req.get("reference_text", ""),
                                                  xvec_only=req.get("xvec_only", False)))

    def voice_asset_path(save_name):
        """Where a design take named save_voice_as is kept, under data/voices; ValueError for a name the Lab will not use and
        FileExistsError for one that is taken."""
        if not isinstance(save_name, str) or not VOICE_ASSET_NAME_RE.fullmatch(save_name):
            raise ValueError("save_voice_as must be a safe name with up to two folders; use letters, digits, underscores, or "
                             "hyphens.")
        asset_root = LAB / "data" / "voices"
        asset_root.mkdir(parents=True, exist_ok=True)
        resolved_root = asset_root.resolve()
        if LAB.resolve() not in resolved_root.parents:
            raise ValueError("Voice asset storage must remain inside voice-lab/data/voices/.")
        save_path = resolved_root.joinpath(*save_name.split("/")).with_suffix(".wav")
        if resolved_root not in save_path.resolve().parents:
            raise ValueError("Voice asset path must remain inside voice-lab/data/voices/.")
        if save_path.exists() or save_path.with_suffix(".json").exists():
            raise FileExistsError(f"Voice asset {save_name!r} already exists; choose another name.")
        return save_path

    @router.post("/api/speak")
    def speak(self, parsed, req):
        if self._no_query(parsed):  # docs/API.md: /api/speak?x=1 is a 404
            return self._json(404, {"error": "not found"})
        save_name = req.get("save_voice_as")
        save_path = None
        if save_name not in (None, ""):
            save_engine = req.get("engine", engine_adapters.DEFAULT_ENGINE)
            designs = engine_adapters.engines_where("saves_designs")
            can_save_design = save_engine in designs and req.get("mode", "design") == "design"
            if not can_save_design:
                return self._json(400, {"error": "Reusable voice assets can be saved from Breeze, Qwen VoiceDesign, or VoxCPM2 "
                                                 "design takes."})
            try:
                save_path = voice_asset_path(save_name)
            except FileExistsError as exc:
                return self._json(409, {"error": str(exc)})
        self._note(engine=req.get("engine", engine_adapters.DEFAULT_ENGINE))
        try:
            wav, sr, info = engines.speak(req)
        except (ValueError, MemoryError) as exc:
            return self._json(400, {"error": str(exc)})
        except WorkerError as exc:
            return self._json(exc.status_code, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        audio_body = synthetic_label.wav_bytes(wav, sr, engine_id=req.get("engine", engine_adapters.DEFAULT_ENGINE))
        headers = {
            "X-Gen-Seconds": f"{info['total_s']:.2f}",
            "X-Audio-Seconds": f"{len(wav) / sr:.2f}",
            "X-First-Chunk": f"{info['first_chunk_s']:.2f}" if info.get("first_chunk_s") else "",
        }
        if save_path is not None:
            try:
                save_designed_reference(save_path, audio_body, req, info, engines.entries.get(req.get("engine"), {}))
            except FileExistsError:
                return self._json(409, {"error": f"Voice asset {save_name!r} already exists; choose another name."})
            headers["X-Voice-Asset-Path"] = str(save_path.resolve())
        if info.get("mode"):
            headers["X-Generation-Mode"] = info["mode"]
        if info.get("profile"):
            headers["X-Decode-Profile"] = info["profile"]
        generation = info.get("generation") or {}
        if generation.get("profile_effective"):
            headers["X-Decode-Profile-Effective"] = generation["profile_effective"]
            headers["X-Profile-Fallback"] = str(bool(generation.get("fallback_to_eager"))).lower()
        if generation.get("max_new_tokens_effective") is not None:
            headers["X-Effective-Max-New-Tokens"] = str(generation["max_new_tokens_effective"])
        if generation.get("audio_frames") is not None:
            headers["X-Audio-Frames"] = str(generation["audio_frames"])
        if generation.get("prefill_tokens") is not None:
            headers["X-Prefill-Tokens"] = str(generation["prefill_tokens"])
        if info.get("reference_cache_hit") is not None:
            headers["X-Reference-Cache-Hit"] = str(bool(info["reference_cache_hit"])).lower()
        if info.get("backend"):
            headers["X-Voice-Backend"] = info["backend"]
        if info.get("checkpoint"):
            headers["X-Model-Checkpoint"] = info["checkpoint"]
        if info.get("checkpoint_revision"):
            headers["X-Checkpoint-Revision"] = info["checkpoint_revision"]
        if info.get("language"):
            headers["X-Voice-Language"] = info["language"]
        if info.get("speaker"):
            headers["X-Voice-Speaker"] = info["speaker"]
        if info.get("sampling"):
            headers["X-Effective-Sampling-Settings"] = json.dumps(info["sampling"], separators=(",", ":"))
        self._send(200, audio_body, "audio/wav", headers)

    @router.post("/api/stream")
    def stream(self, parsed, req):
        started = time.perf_counter()
        self._note(engine=req.get("engine"))
        save_path = None
        save_name = req.get("save_voice_as")
        if save_name not in (None, ""):
            streamed_designs = engine_adapters.engines_where("streams") & engine_adapters.engines_where("saves_designs")
            if req.get("engine") not in streamed_designs or req.get("mode", "design") != "design":
                return self._json(400, {"error": "Only a Qwen VoiceDesign stream can be saved as a reusable voice reference."})
            try:
                save_path = voice_asset_path(save_name)
            except FileExistsError as exc:
                return self._json(409, {"error": str(exc)})

        state = {"headers_sent": False, "first_byte_s": None, "first_audio_s": None,
                 "chunks": [], "sample_count": 0, "next_sequence": 0, "sample_rate": None}

        def begin_stream(meta):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Voice-Lab-Stream", "native-generator")
            self.end_headers()
            state["headers_sent"] = True
            state["first_byte_s"] = time.perf_counter() - started
            try:
                self._stream_line({"type": "start", **meta, "service_first_byte_s": state["first_byte_s"]})
            except OSError as exc:
                raise ConnectionAbortedError("Client disconnected before audio generation started.") from exc

        def send_audio(packet):
            if state["sample_rate"] is not None and packet["sample_rate"] != state["sample_rate"]:
                raise RuntimeError("Stream sample rate changed after the response started.")
            if (packet["sequence"] != state["next_sequence"] or
                    packet["sample_start"] != state["sample_count"]):
                raise RuntimeError("Stream sample offsets are discontinuous.")
            state["sample_rate"] = packet["sample_rate"]
            state["sample_count"] += packet["sample_count"]
            state["next_sequence"] += 1
            if save_path is not None:
                state["chunks"].append(packet["pcm"])
            event = {"type": "audio", "sequence": packet["sequence"],
                     "sample_start": packet["sample_start"], "sample_count": packet["sample_count"],
                     "sample_rate": packet["sample_rate"], "sample_format": "pcm_s16le",
                     "data": packet["data"], "service_first_audio_s": None}
            if packet["sequence"] == 0:
                state["first_audio_s"] = time.perf_counter() - started
                event["service_first_audio_s"] = state["first_audio_s"]
                event["generator_first_chunk_s"] = packet.get("generator_first_chunk_s")
                event["backend_chunk_index"] = packet.get("backend_chunk_index")
            try:
                self._stream_line(event)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                return False
            return True

        def stream_error(text):
            try:
                self._stream_line({"type": "error", "error": text})
            except OSError:
                pass

        try:
            result = engines.stream_qwen(req, begin_stream, send_audio)
            saved_asset = None
            if save_path is not None:
                import numpy as np
                raw_pcm = b"".join(state["chunks"])
                if len(raw_pcm) != state["sample_count"] * 2:
                    raise RuntimeError("Stream capture byte count did not match delivered samples.")
                audio = np.frombuffer(raw_pcm, dtype="<i2")
                save_designed_reference(save_path, synthetic_label.wav_bytes(audio, int(result["sample_rate"]),
                                                                             engine_id=req.get("engine")),
                                        req, result, engines.entries.get(req.get("engine"), {}))
                saved_asset = str(save_path.resolve())
            self._stream_line({
                "type": "complete", "engine": result.get("engine"), "mode": result.get("mode"),
                "streaming_kind": result.get("streaming_kind", "native_generator"),
                "chunk_count": result.get("chunk_count"), "backend_chunk_count": result.get("backend_chunk_count"),
                "sample_count": result.get("sample_count"), "sample_rate": result.get("sample_rate"),
                "audio_seconds": (result.get("sample_count", 0) / result["sample_rate"] if result.get("sample_rate") else None),
                "generator_first_chunk_s": result.get("generator_first_chunk_s"),
                "service_first_byte_s": state["first_byte_s"],
                "service_first_audio_s": state["first_audio_s"],
                "generation_total_s": result.get("total_s"),
                "service_total_s": time.perf_counter() - started,
                "sampling": result.get("sampling"), "backend": result.get("backend"),
                "checkpoint": result.get("checkpoint"),
                "checkpoint_revision": result.get("checkpoint_revision"),
                "reference_cache_hit": result.get("reference_cache_hit"),
                "speaker": result.get("speaker"), "saved_asset": saved_asset,
            })
        except (ValueError, MemoryError) as exc:
            if not state["headers_sent"]:
                return self._json(400, {"error": str(exc)})
            stream_error(str(exc))
        except WorkerError as exc:
            if not state["headers_sent"]:
                return self._json(exc.status_code, {"error": str(exc)})
            stream_error(str(exc))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass  # a disconnected consumer has already cleared its own pending browser audio
        except Exception as exc:
            if not state["headers_sent"]:
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            stream_error(f"{type(exc).__name__}: {exc}")
        self.close_connection = True

    @router.post("/api/performance")
    def performance(self, parsed, req):
        identity = cast.get_generation_identity(req.get("identity_id"), req.get("reference_version_id"))
        engine_id = identity["engine_id"]
        self._note(engine=engine_id)
        voice_identity = generation_identity_snapshot(identity)
        if req.get("identity_snapshot") is not None and req["identity_snapshot"] != voice_identity:
            raise ValueError("The scene voice identity changed after this batch was saved; reload and save the scene before rendering.")
        language = req.get("language") or (identity.get("engine_options") or {}).get("language") or "English"
        original_text = req.get("text")
        if not isinstance(original_text, str) or not original_text.strip() or len(original_text) > 1200:
            raise ValueError("Performance text must contain 1–1200 characters.")
        spoken_text = original_text
        if req.get("use_pronunciations") is True:
            spoken_text = cast.apply_pronunciations(original_text, identity["character_id"])
        if len(spoken_text) > 1200:
            raise ValueError("Pronunciation replacements expanded the spoken line past 1200 characters.")
        direction = req.get("direction", (identity.get("engine_options") or {}).get("default_direction", ""))
        if not isinstance(direction, str) or len(direction) > 1000:
            raise ValueError("Acting direction must be text of 1000 characters or fewer.")
        adapter = engine_adapters.get(engine_id)
        if direction.strip() and engine_id not in DIRECTION_ENGINES:
            if adapter is not None and adapter.direction_refusal:
                raise ValueError(adapter.direction_refusal)
            raise ValueError(f"{engine_id} has no verified text-direction control; use its native event tags in the line text.")
        settings = req.get("settings", {})
        if not isinstance(settings, dict):
            raise ValueError("Performance settings must be an object.")
        applied_settings = effective_settings(engine_id, settings)
        backend_request = {"engine": engine_id, "text": spoken_text}
        if identity["kind"] == "reference":
            reference = identity["reference"]
            backend_request["reference_path"] = reference["audio_path"]
            if getattr(adapter, "takes_transcript", False):
                backend_request["reference_text"] = reference["transcript"]
        if adapter is not None:
            backend_request.update(adapter.performance_request(identity, settings, applied_settings, direction, language))
        line_id = req.get("line_id")
        if not isinstance(line_id, str) or not line_id.strip() or len(line_id) > 100:
            raise ValueError("Line ID must contain 1–100 characters.")

        request_key = req.get("idempotency_key")
        if request_key is not None:
            if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
                raise ValueError("Performance idempotency_key must contain 1–200 characters.")
            existing = cast.get_take_by_request_key(request_key)
            if existing is not None:
                previous_settings = dict(existing["settings"])
                previous_pronunciations = previous_settings.pop("use_pronunciations", False)
                expected_reference = identity["reference"]["id"] if identity["reference"] else None
                if (existing["parent_take_id"] is not None or  # a key an edit of a take used is not a performance's
                        existing["identity_id"] != identity["id"] or existing["reference_version_id"] != expected_reference or
                        existing["line_id"] != line_id.strip() or existing["text"] != original_text.strip() or
                        existing["spoken_text"] != spoken_text.strip() or existing["language"] != language or
                        existing["direction"] != direction.strip() or
                        previous_pronunciations != (req.get("use_pronunciations") is True) or
                        effective_settings(engine_id, previous_settings) != applied_settings or
                        existing["model"].get("voice_identity") not in (None, voice_identity)):
                    raise ValueError("Performance idempotency_key was already used with different inputs.")
                audio_body = Path(cast.get_media_path("take", existing["id"])).read_bytes()
                return self._send(200, audio_body, "audio/wav", {
                    "X-Take-Id": existing["id"], "X-Cast-Character": existing["character_name"],
                    "X-Cast-Identity": existing["identity_label"], "X-Cast-Line-Id": existing["line_id"],
                    "X-Idempotent-Replay": "true",
                })

        wav, sample_rate, engine_info = engines.speak(backend_request)
        # Kokoro's phoneme times are stored in their own take column, not in the engine record saved below.
        phoneme_timing = engine_info.pop("timing", None)
        catalog_entry = engines.entries[engine_id]
        voice_options = identity.get("engine_options") or {}
        if adapter is not None:
            generation_mode = adapter.mode_for(backend_request)
        else:
            generation_mode = "fixed_voice"
        model = {
            "engine_id": engine_id, "engine_name": catalog_entry.get("name", engine_id),
            "backend": engine_info.get("backend", catalog_entry.get("backend", {}).get("name")),
            "checkpoint": engine_info.get("checkpoint", catalog_entry.get("checkpoint")),
            "checkpoint_revision": engine_info.get("checkpoint_revision", catalog_entry.get("local_revision")),
            "runtime": catalog_entry.get("runtime"), "mode": generation_mode,
            "backend_result": engine_info,
            "effective_settings": applied_settings,
            "voice_identity": voice_identity,
        }
        if adapter is not None:
            model.update(adapter.take_record(voice_options))
        take = cast.create_take(
            identity, line_id=line_id.strip(), text=original_text.strip(), spoken_text=spoken_text.strip(),
            language=language, direction=direction.strip(),
            settings={**applied_settings, "use_pronunciations": req.get("use_pronunciations") is True},
            model=model, wav=wav, sample_rate=sample_rate, generation_info=engine_info,
            request_key=request_key, phoneme_timing=phoneme_timing,
        )
        audio_body = Path(cast.get_media_path("take", take["id"])).read_bytes()
        headers = {"X-Take-Id": take["id"], "X-Cast-Character": identity["character_name"],
                   "X-Cast-Identity": identity["label"], "X-Cast-Line-Id": take["line_id"],
                   "X-Gen-Seconds": str(engine_info.get("total_s", "")),
                   "X-Audio-Seconds": f"{take['duration_seconds']:.2f}",
                   "X-Voice-Backend": str(model.get("backend") or ""),
                   "X-Model-Checkpoint": str(model.get("checkpoint") or ""),
                   "X-Checkpoint-Revision": str(model.get("checkpoint_revision") or ""),
                   "X-Generation-Mode": generation_mode}
        return self._send(200, audio_body, "audio/wav", headers)

    @router.post("/api/kokoro/phonemize")
    def kokoro_phonemize(self, parsed, req):
        self._json(200, engines.phonemize(req.get("text", ""), req.get("language", "English")))

    @router.post("/api/convert")
    def convert(self, parsed, req):
        source_path, source_info = _conversion_audio_path(req.get("source_path"), "Source performance", target=False, cast=cast)
        target_path, target_info = _conversion_audio_path(req.get("target_reference_path"), "Target voice reference",
                                                          target=True, cast=cast)
        if source_path == target_path:
            raise ValueError("Source performance and target voice reference must be separate files.")
        reference_id = req.get("target_reference_id")
        if reference_id:
            reference = cast.get_reference(reference_id)
            recorded_target = Path(cast.get_media_path("reference", reference_id)).resolve()
            if recorded_target != target_path:
                raise ValueError("Target reference path does not match the selected Cast reference version.")
            target_metadata = {"reference_version_id": reference["id"], "origin": reference.get("origin", ""),
                               "allowed_use": reference.get("allowed_use", "")}
        else:
            target_metadata = next((row for row in _conversion_asset_rows(cast, target=True)
                                    if Path(row["path"]).resolve() == target_path), {})
        try:
            wav, sample_rate, engine_info = engines.convert(str(source_path), str(target_path))
        except WorkerError:
            raise
        import numpy as np
        audio = np.asarray(wav, dtype=np.float32).reshape(-1)
        if not audio.size or not np.isfinite(audio).all():
            raise RuntimeError("Chatterbox voice conversion produced empty or invalid audio.")
        source_upload = _upload_record(source_path) or {}  # a file chosen from this computer: the name it had there
        conversion_id = uuid.uuid4().hex
        artifact_dir = OUT / "conversions" / conversion_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        audio_path = artifact_dir / "converted.wav"
        manifest_path = artifact_dir / "manifest.json"
        wav_bytes = synthetic_label.wav_bytes(audio, int(sample_rate), engine_id=engine_adapters.only("converts"))
        with audio_path.open("xb") as output:
            output.write(wav_bytes)
        manifest = {
            "schema_version": 1, "conversion_id": conversion_id,
            "created_at_local": datetime.now().astimezone().isoformat(),
            "task": "voice_conversion", "watermark": "PerthImplicitWatermarker",
            "model": engine_info,
            "source_performance": {
                "path": str(source_path), "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "sample_rate_hz": source_info.samplerate,
                "duration_seconds": round(float(source_info.duration), 4),
                **({"cast_take_id": next((row.get("take_id") for row in _conversion_asset_rows(cast)
                                         if Path(row["path"]).resolve() == source_path), None)}),
                **({"original_name": source_upload["original_name"]} if source_upload.get("original_name") else {}),
            },
            "target_voice_reference": {
                "path": str(target_path), "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                "sample_rate_hz": target_info.samplerate,
                "duration_seconds": round(float(target_info.duration), 4),
                **target_metadata,
            },
            "output": {
                "path": str(audio_path.resolve()), "sha256": hashlib.sha256(wav_bytes).hexdigest(),
                "sample_rate_hz": int(sample_rate), "duration_seconds": len(audio) / int(sample_rate),
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        audio_url = "/out/" + str(audio_path.resolve().relative_to(OUT.resolve())).replace("\\", "/")
        self._json(201, {"conversion": {
            "conversion_id": conversion_id, "audio_url": audio_url,
            "audio_path": str(audio_path.resolve()), "manifest_path": str(manifest_path.resolve()),
            "manifest": manifest,
        }})

    # ------------------------------------------------------------------------------------------------ Cast

    @router.get("/api/cast")
    def cast_list(self, parsed, req):
        self._json(200, {"schema_version": 1, "characters": cast.list_cast()})

    @router.post("/api/characters")
    def create_character(self, parsed, req):
        self._json(201, {"character": cast.create_character(req.get("name"), req.get("description", ""), req.get("tags"))})

    @router.post("/api/characters/{character_id}")
    def update_character(self, parsed, req, character_id):
        self._json(200, {"character": cast.update_character(character_id, req)})

    @router.post("/api/characters/{character_id}/identities")
    def create_identity(self, parsed, req, character_id):
        engine_id = req.get("engine_id")
        entry = engines.api_entries.get(engine_id)
        if entry is None:
            raise ValueError("Choose an engine exposed by the local service.")
        capabilities = entry.get("capabilities", {})
        clone_status = capabilities.get("voice_cloning", {}).get("status")
        fixed_voice = capabilities.get("voice_selection", {}).get("status") == "supported"
        preset_voice = (engine_id in PRESET_VOICE_ENGINES and
                        capabilities.get("voice_selection", {}).get("status") in {"supported", "worker_verified"})
        adapter = engine_adapters.get(engine_id)
        fixed_voices = adapter is not None and adapter.fixed_voices and fixed_voice
        if clone_status not in {"supported", "worker_verified"} and not fixed_voices and not preset_voice:
            raise ValueError("This model does not provide a reusable clone reference or a fixed voice selector.")
        if adapter is not None and (adapter.fixed_voices or adapter.preset_voices):
            options = adapter.identity_options(capabilities, req, performance_controls(engine_id, capabilities), engines)
        else:
            options = {}
        self._json(201, {"identity": cast.create_identity(character_id, engine_id, req.get("label"), options,
                                                          req.get("notes", ""))})

    @router.post("/api/identities/{identity_id}/update")
    def update_identity(self, parsed, req, identity_id):
        self._json(200, {"identity": cast.update_identity(identity_id, req, fixed_voices=discover_kokoro_voices())})

    @router.post("/api/identities/{identity_id}/references/preview")
    def preview_reference(self, parsed, req, identity_id):
        try:
            body = self._read_body(MAX_UPLOAD_BYTES)
            metadata = self._reference_metadata()
            audio, metrics = cast.preview_reference(identity_id, metadata.get("filename"), body, edits=metadata.get("edits"))
        except RuntimeError as exc:
            return self._json(400, {"error": str(exc)})
        quality = json.dumps(metrics["quality"], separators=(",", ":")).encode("ascii")
        self._send(200, audio, "audio/wav", {"X-Reference-Duration": f"{metrics['duration_seconds']:.3f}",
                                             "X-Reference-Sample-Rate": str(metrics["sample_rate"]),
                                             "X-Reference-RMS": f"{metrics['rms_dbfs']:.1f}",
                                             "X-Reference-Quality": base64.urlsafe_b64encode(quality).decode("ascii")})

    @router.post("/api/identities/{identity_id}/references")
    def upload_reference(self, parsed, req, identity_id):
        try:
            body = self._read_body(MAX_UPLOAD_BYTES)
            metadata = self._reference_metadata()
            saved = cast.add_reference(identity_id, metadata.get("filename"), body,
                                       transcript=metadata.get("transcript", ""), origin=metadata.get("origin", ""),
                                       allowed_use=metadata.get("allowed_use", ""), edits=metadata.get("edits"))
        except RuntimeError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(201, {"reference": saved})

    @router.post("/api/identities/{identity_id}/references/from-take")
    def reference_from_take(self, parsed, req, identity_id):
        self._json(201, {"reference": cast.add_reference_from_take(
            identity_id, req.get("take_id"), transcript=req.get("transcript"), edits=req.get("edits"),
            origin=req.get("origin"), allowed_use=req.get("allowed_use"))})

    @router.post("/api/references/{reference_id}/versions")
    def reference_version(self, parsed, req, reference_id):
        self._json(201, {"reference": cast.add_reference_version(reference_id, req)})

    @router.get("/api/references/{reference_id}/audio")
    def reference_version_audio(self, parsed, req, reference_id):
        try:
            target = cast.get_media_path("reference", reference_id)
            self._send(200, target.read_bytes(), "audio/wav", {"X-Reference-Version": reference_id})
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/references/{reference_id}/source")
    def reference_version_source(self, parsed, req, reference_id):
        try:
            target = cast.get_media_path("reference_source", reference_id)
            content_type = {".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
                            ".m4a": "audio/mp4"}.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), content_type, {"X-Reference-Version": reference_id})
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/references/{reference_id}/transcript-suggestion")
    def transcript_suggestion(self, parsed, req, reference_id):
        # A draft only: nothing is saved or approved until the user checks and saves the text.
        try:
            audio = cast.get_media_path("reference", reference_id)
            self._json(200, {"suggestion": transcript_suggest.suggest(audio)})
        except (KeyError, FileNotFoundError) as exc:
            self._json(404, {"error": str(exc)})
        except transcript_suggest.TranscriptBusy as exc:
            self._json(409, {"error": str(exc)})
        except transcript_suggest.TranscriptUnavailable as exc:
            self._json(503, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(500, {"error": str(exc)})

    @router.get("/api/pronunciations")
    def pronunciation_list(self, parsed, req):
        character_id = (parse_qs(parsed.query).get("character_id") or [None])[0]
        self._json(200, {"pronunciations": cast.list_pronunciations(character_id)})

    @router.post("/api/pronunciations")
    def save_pronunciation(self, parsed, req):
        self._json(200, {"pronunciation": cast.save_pronunciation(
            source=req.get("source"), replacement=req.get("replacement"), character_id=req.get("character_id"),
            notes=req.get("notes", ""))})

    @router.post("/api/retirements")
    def retire(self, parsed, req):
        self._json(201, cast.retire(req.get("kind"), req.get("id"), reason=req.get("reason", "")))

    @router.post("/api/retirements/restore")
    def restore_retired(self, parsed, req):
        self._json(200, cast.restore(req.get("kind"), req.get("id")))

    @router.get("/api/purges/preview")
    def purge_preview(self, parsed, req):
        params = parse_qs(parsed.query)
        try:
            self._json(200, cast.purge_preview((params.get("kind") or [None])[0], (params.get("id") or [None])[0]))
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    @router.post("/api/purges")
    def purge(self, parsed, req):
        self._json(200, cast.purge(req.get("kind"), req.get("id"), reason=req.get("reason", ""), confirm=req.get("confirm")))

    # ------------------------------------------------------------------------------------------------ takes

    @router.get("/api/takes")
    def take_list(self, parsed, req):
        params = parse_qs(parsed.query)
        self._json(200, {"takes": cast.list_takes((params.get("identity_id") or [None])[0], (params.get("line_id") or [None])[0])})

    @router.get("/api/takes/{take_id}")
    def take(self, parsed, req, take_id):
        try:
            self._json(200, {"take": cast.get_take(take_id)})
        except (ValueError, KeyError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/takes/{take_id}/audio")
    def take_audio(self, parsed, req, take_id):
        try:
            target = cast.get_media_path("take", take_id)
            self._send(200, target.read_bytes(), "audio/wav", {"X-Take-Id": take_id})
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/takes/{take_id}/waveform")
    def take_waveform(self, parsed, req, take_id):
        try:
            self._json(200, cast.take_waveform(take_id, (parse_qs(parsed.query).get("points") or ["600"])[0]))
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    @router.get("/api/takes/{take_id}/voice-match")
    def take_voice_match(self, parsed, req, take_id):
        try:
            self._json(200, {"voice_match": speaker_similarity.voice_match(cast, take_id)})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(503, {"error": str(exc)})

    @router.post("/api/takes/{take_id}/update")
    def update_take(self, parsed, req, take_id):
        self._json(200, {"take": cast.set_take_state(take_id, status=req.get("status"), rating=req.get("rating"),
                                                     notes=req.get("notes"))})

    @router.post("/api/takes/{take_id}/edit")
    def edit_take(self, parsed, req, take_id):
        edit_key = req.pop("idempotency_key", None)
        take, replayed = cast.create_edited_take(take_id, req, request_key=edit_key)
        self._json(200 if replayed else 201, {"take": take, "replayed": replayed})

    @router.post("/api/takes/{take_id}/edit-preview")
    def preview_take_edit(self, parsed, req, take_id):
        audio, report = cast.preview_edit(take_id, req)
        self._send(200, audio, "audio/wav", {
            "X-Audio-Seconds": f"{report['duration_seconds']:.2f}",
            "X-Edit-Start-Seconds": f"{report['start_seconds']:.3f}",
            "X-Edit-End-Seconds": f"{report['end_seconds']:.3f}",
            "X-Peak-Dbfs": "" if report["peak_dbfs"] is None else f"{report['peak_dbfs']:.2f}"})

    # ------------------------------------------------------------------------------------------------ scenes

    @router.get("/api/scenes")
    def scene_list(self, parsed, req):
        self._json(200, {"schema_version": 1, "scenes": scenes.list_scenes()})

    @router.post("/api/scenes")
    def create_scene(self, parsed, req):
        self._json(201, {"scene": scenes.create_scene(req)})

    @router.get("/api/scenes/template.json")
    def scene_template_json(self, parsed, req):
        self._json(200, json_template())

    @router.get("/api/scenes/template.csv")
    def scene_template_csv(self, parsed, req):
        self._send(200, csv_template(), "text/csv; charset=utf-8",
                   {"Content-Disposition": "attachment; filename=voice-lab-scene-template.csv"})

    def _import_content(req):
        content = req.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_SCENE_REQUEST_BYTES:
            raise ValueError("Scene import content must be UTF-8 text up to 2 MiB.")
        return content

    @router.post("/api/scenes/import/preview")
    def preview_script(self, parsed, req):
        self._json(200, {"script": parse_script(_import_content(req), req.get("format"))})

    @router.post("/api/scenes/import")
    def import_scene(self, parsed, req):
        fmt = req.get("format")
        content = _import_content(req)
        if fmt in ("text", "fountain"):
            manifest, report = scenes.script_manifest(content, fmt, req.get("speakers"), req.get("narrator"), req.get("name"))
            self._json(201, {"scene": scenes.create_scene(manifest), "report": report})
            return
        if fmt == "json":
            try:
                manifest = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid scene JSON: {exc}") from exc
        elif fmt == "csv":
            manifest = scenes.import_csv(content.encode("utf-8"))
        else:
            raise ValueError("Scene import format must be json, csv, text or fountain.")
        self._json(201, {"scene": scenes.create_scene(manifest)})

    @router.get("/api/scenes/{scene_id}")
    def scene(self, parsed, req, scene_id):
        try:
            self._json(200, {"scene": scenes.get_scene(scene_id)})
        except (ValueError, KeyError, RuntimeError) as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/scenes/{scene_id}")
    def update_scene(self, parsed, req, scene_id):
        self._json(200, {"scene": scenes.update_scene(scene_id, req, expected_revision=req.get("expected_revision"))})

    @router.get("/api/scenes/{scene_id}/export.json")
    def scene_export_json(self, parsed, req, scene_id):
        try:
            self._send(200, scenes.export_json(scene_id), "application/json; charset=utf-8",
                       {"Content-Disposition": f"attachment; filename=scene-{scene_id}.json"})
        except (ValueError, KeyError, RuntimeError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/scenes/{scene_id}/export.csv")
    def scene_export_csv(self, parsed, req, scene_id):
        try:
            self._send(200, scenes.export_csv(scene_id), "text/csv; charset=utf-8",
                       {"Content-Disposition": f"attachment; filename=scene-{scene_id}.csv"})
        except (ValueError, KeyError, RuntimeError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/scenes/{scene_id}/previews/{preview_file}.wav")
    def scene_preview_audio(self, parsed, req, scene_id, preview_file):
        # Preview audio lives in the scene store's own output folder, which need not be the project's out/.
        folder = (scenes.output_root / scene_id).resolve()
        target = (folder / f"{preview_file}.wav").resolve()
        if target.parent == folder and target.is_file():
            self._send(200, target.read_bytes(), "audio/wav")
        else:
            self._json(404, {"error": "Scene preview not found."})

    @router.get("/api/scenes/{scene_id}/segments")
    def scene_segments(self, parsed, req, scene_id):
        line_id = (parse_qs(parsed.query).get("line_id") or [None])[0]
        try:
            self._json(200, {"segments": scenes.segment_preview(scene_id, line_id)})
        except (ValueError, KeyError, RuntimeError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/scenes/{scene_id}/jobs")
    def scene_jobs(self, parsed, req, scene_id):
        try:
            self._json(200, {"jobs": scenes.list_jobs(scene_id)})
        except (ValueError, KeyError) as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/scenes/{scene_id}/jobs")
    def create_scene_job(self, parsed, req, scene_id):
        job = scenes.create_job(scene_id, req.get("line_ids"), req.get("segment_ids"))
        batches.wake()
        self._json(202, {"job": job})

    @router.post("/api/scenes/{scene_id}/select")
    def select_scene_take(self, parsed, req, scene_id):
        self._json(200, {"scene": scenes.select_take(scene_id, req.get("line_id"), req.get("take_id"), req.get("segment_id"))})

    @router.get("/api/scenes/{scene_id}/lines")
    def scene_lines(self, parsed, req, scene_id):
        try:
            self._json(200, scenes.line_states(scene_id))
        except (ValueError, KeyError, RuntimeError) as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/scenes/{scene_id}/choose-newest")
    def choose_newest_scene_takes(self, parsed, req, scene_id):
        scene, chosen, lines = scenes.choose_newest_takes(scene_id, req.get("line_ids"))
        self._json(200, {"scene": scene, "chosen": chosen, "lines": lines})

    @router.post("/api/scenes/{scene_id}/preview")
    def scene_preview(self, parsed, req, scene_id):
        preview = scenes.assemble_preview(scene_id)
        preview_url = f"/api/scenes/{scene_id}/previews/"
        preview["audio_url"] = preview_url + Path(preview["audio_path"]).name
        preview["mastered_audio_url"] = preview_url + Path(preview["mastered_audio_path"]).name
        self._json(201, {"preview": preview})

    @router.get("/api/scenes/jobs/{job_id}")
    def scene_job(self, parsed, req, job_id):
        try:
            self._json(200, {"job": scenes.get_job(job_id)})
        except (ValueError, KeyError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/scenes/jobs/{job_id}/manifest.json")
    def scene_job_manifest(self, parsed, req, job_id):
        try:
            valid, info = scenes.verify_job_manifest(job_id)
            if not valid:
                self._json(409, {"error": "Batch manifest checksum mismatch.", "details": info})
            else:
                self._send(200, Path(info["path"]).read_bytes(), "application/json; charset=utf-8")
        except (ValueError, KeyError) as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/scenes/jobs/{job_id}/pause", "/api/scenes/jobs/{job_id}/resume", "/api/scenes/jobs/{job_id}/cancel",
                 "/api/scenes/jobs/{job_id}/retry")
    def scene_job_action(self, parsed, req, job_id):
        action = parsed.path.rsplit("/", 1)[1]
        job = {"pause": scenes.request_pause, "resume": scenes.resume, "cancel": scenes.cancel,
               "retry": scenes.retry_failed}[action](job_id)
        if action == "cancel" and (req or {}).get("stop_current") is True:
            # Cancel also stops the line the batch is rendering now, instead of letting it finish.
            request_id = batches.current_request(job_id)
            if request_id and hasattr(engines, "cancel_request"):
                try:
                    engines.cancel_request(request_id)
                except (KeyError, ValueError):
                    pass  # it finished, or had not reached the worker, in the meantime
        batches.wake()
        self._json(200, {"job": job})

    # ------------------------------------------------------------------------------------------------ export packages

    @router.get("/api/exports")
    def export_list(self, parsed, req):
        # ?recheck=1 asks FFmpeg again, for a page whose user has just installed it.
        recheck = parse_qs(parsed.query).get("recheck") == ["1"]
        self._json(200, {"exports": export_panel.list_exports(scenes.root), "ffmpeg": export_panel.ffmpeg_report(refresh=recheck)})

    @router.get("/api/ffmpeg/install")
    def ffmpeg_install_status(self, parsed, req):
        self._json(200, {"install": installer.snapshot("tool")})

    @router.post("/api/ffmpeg/install")
    def ffmpeg_install(self, parsed, req):
        dry_run = req.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be true or false.")
        if dry_run:
            return self._json(200, {"plan": installer.plan("ffmpeg", tool=True)})
        self._json(200, {"install": installer.start("ffmpeg", tool=True)})

    @router.post("/api/exports/preview")
    def export_preview(self, parsed, req):
        self._json(200, export_panel.run(scenes.root, req, dry_run=True))

    @router.post("/api/exports")
    def export(self, parsed, req):
        try:
            self._json(201, export_panel.run(scenes.root, req))
        except export_panel.ExportBusy as exc:
            self._json(409, {"error": str(exc)})

    @router.post("/api/exports/{export_id}/verify")
    def verify_export(self, parsed, req, export_id):
        self._json(200, export_panel.verify(scenes.root, export_id))

    # ------------------------------------------------------------------------------------------------ comparisons

    @router.get("/api/evaluations/suites")
    def evaluation_suites(self, parsed, req):
        self._json(200, list_suites())

    @router.get("/api/evaluations")
    def evaluation_runs(self, parsed, req):
        self._json(200, {"runs": evaluations.list_runs()})

    @router.get("/api/evaluations/runs/{run_id}")
    def evaluation_run(self, parsed, req, run_id):
        try:
            self._json(200, {"run": evaluations.get_run(run_id)})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/evaluations/runs/import")
    def import_evaluation_run(self, parsed, req):
        self._json(201, {"run": evaluations.register_run_manifest(req.get("manifest_path", ""))})

    @router.post("/api/evaluations/cast-import")
    def import_cast_comparison(self, parsed, req):
        self._json(201, {"run": import_cast_takes(cast, evaluations, req.get("take_ids"),
                                                  title=req.get("title", "Saved voice comparison"))})

    @router.get("/api/evaluations/blind-sessions")
    def blind_sessions(self, parsed, req):
        self._json(200, {"sessions": evaluations.list_blind_sessions()})

    @router.post("/api/evaluations/blind-sessions")
    def create_blind_session(self, parsed, req):
        self._json(201, {"session": evaluations.create_blind_session(req.get("run_id"), req.get("evaluator", "local listener"))})

    @router.get("/api/evaluations/blind-sessions/{session_id}")
    def blind_session(self, parsed, req, session_id):
        try:
            self._json(200, {"session": evaluations.get_blind_session(session_id)})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/evaluations/blind-sessions/{session_id}/ratings")
    def rate_blind_session(self, parsed, req, session_id):
        self._json(200, {"session": evaluations.save_rating(session_id, req.get("trial_id"), req.get("ratings"),
                                                            req.get("notes", ""))})

    @router.post("/api/evaluations/blind-sessions/{session_id}/reveal")
    def reveal_blind_session(self, parsed, req, session_id):
        self._json(200, {"session": evaluations.reveal_blind_session(session_id)})

    @router.get("/api/evaluations/blind-audio/{item_id}")
    def blind_audio(self, parsed, req, item_id):
        try:
            self._send(200, evaluations.get_blind_audio_path(item_id).read_bytes(), "audio/wav")
        except (KeyError, FileNotFoundError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/evaluations/items/{item_id}")
    def evaluation_item(self, parsed, req, item_id):
        try:
            self._json(200, {"item": evaluations.get_item(item_id)})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/evaluations/items/{item_id}/transcript")
    def item_transcript(self, parsed, req, item_id):
        self._json(200, evaluations.add_transcript(item_id, req.get("heard_text"), req.get("evaluator")))

    # ------------------------------------------------------------------------------------------------ Drama studio

    @router.get("/api/drama3/*", "/api/drama3/info", "/api/drama3/jobs/{job_id}", "/api/drama3/jobs/{job_id}/audio",
                "/api/drama3/jobs/{job_id}/metadata", "/api/drama3/jobs/{job_id}/package")
    def drama_read(self, parsed, req, job_id=None, rest=None):
        from drama3_production import capabilities
        try:
            studio = drama()
            suffix = parsed.path[len("/api/drama3/"):]
            if suffix == "info":
                return self._json(200, {**capabilities(), "sources": studio.sources(), "jobs": studio.list()})
            match = re.fullmatch(r"jobs/([a-f0-9]{32})(?:/(audio|metadata|package))?", suffix)
            if not match:
                return self._json(404, {"error": "Drama endpoint not found"})
            if match.group(2):
                data, ctype = studio.artifact(*match.groups())
                disposition = ("inline" if match.group(2) == "audio" else
                               "attachment; filename=drama-take." + ("zip" if match.group(2) == "package" else "json"))
                return self._send(200, data, ctype, {"Content-Disposition": disposition})
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            self._json(200, studio.get(match.group(1), offset))
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})

    @router.post("/api/drama3/jobs")
    def start_drama_job(self, parsed, req):
        self._json(202, {"job": drama().start(req)})

    @router.post("/api/drama3/jobs/{job_id}/cancel")
    def cancel_drama_job(self, parsed, req, job_id):
        self._json(200, {"job": drama().cancel(job_id)})

    router.check()
    return type("Handler", (Handler,), {"lab": lab, "router": router})


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # A page asks for its ten scripts side by side, and while the server is busy for a moment they wait in the listen queue.
    # The default of 5 is too short for that: Windows refuses the connection that finds the queue full, so the browser shows
    # ERR_CONNECTION_REFUSED for that script and the page comes up with a part missing.
    request_queue_size = 128


def preload(engines, keys):
    """Load and warm engines in the background so the first real request is quick."""
    for key in keys:
        try:
            engines.load(key)
        except Exception as exc:
            print(f"Preload of {key} failed: {type(exc).__name__}: {exc}", flush=True)


def measure_old_takes(cast, log):
    """Give takes made before loudness readings existed their readings, in the background at start. Never raises."""
    try:
        result = cast.measure_missing_loudness()
    except Exception as exc:
        log.event(f"loudness readings for older takes stopped: {type(exc).__name__}: {exc}")
        return
    if result["measured"] or result["failed"]:
        log.event(f"loudness readings added to {result['measured']} older takes"
                  + (f"; {result['failed']} could not be read" if result["failed"] else ""))


def exit_with_parent(pid, engines, server=None):
    """Stop when the process that started us is gone, so no service is left holding the GPU."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)      # SYNCHRONIZE
    if not handle:
        print(f"Parent process {pid} is not running; stopping.", flush=True)
        if server is not None:
            server.shutdown()
            return
        engines.close()
        os._exit(0)
    try:
        while kernel32.WaitForSingleObject(handle, 1000) == 258:  # WAIT_TIMEOUT
            pass
    finally:
        kernel32.CloseHandle(handle)
    print("Parent process ended; stopping the voice service.", flush=True)
    if server is not None:
        server.shutdown()
    else:
        engines.close()
        os._exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="auto", choices=DEVICES,
                        help="auto (the default) uses a graphics card when there is one; cpu or cuda chooses")
    parser.add_argument("--idle-minutes", type=float, default=10, help="unload models after this long idle (0 = never)")
    parser.add_argument("--state-root", default=str(LAB), help="local Cast/Scene data root (defaults to this project)")
    parser.add_argument("--preload", default="", help="comma list of engines to load at start (kokoro, turbo, orig)")
    parser.add_argument("--parent-pid", type=int, default=0, help="exit when this process ends (used by a parent launcher)")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="do not sweep stale worker folders, desktop sessions and old service logs from out/ at start")
    parser.add_argument("--desktop-session", type=Path, help=argparse.SUPPRESS)
    # Where this run's service log goes instead of out/logs; the tests use it to leave the real out/ folder alone.
    parser.add_argument("--log-dir", type=Path, default=OUT / "logs", help=argparse.SUPPRESS)
    args = parser.parse_args()
    folder_notes = migrate_program_folders(LAB)
    device = pick_device(args.device)
    state_root = Path(args.state_root).expanduser().resolve()
    cast = CastStore(state_root)
    engines = EnginePool(device, args.idle_minutes,
                         resource_check=lambda key: require_ram(MIN_FREE_RAM_GB[key], key),
                         reference_roots=(state_root / "data" / "cast" / "media" / "references",),
                         conversion_source_roots=(state_root / "data" / "cast" / "media" / "takes",
                                                  state_root / "out" / "scenes"),
                         media_guard=cast.assert_media_usable,
                         check_store=EngineCheckStore(state_root / "data" / "engines" / "checks.json"),
                         device_store=EngineDeviceStore(state_root / "data" / "engines" / "devices.json"),
                         ram_needs=MIN_FREE_RAM_GB, ram_probe=ram_report)
    scenes = SceneStore(cast, engines.entries, root=state_root)
    evaluations = EvaluationStore(state_root / "data/evaluations/evaluations.sqlite3",
                                  lab_root=state_root)
    server = LocalThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler)
    batch_runner = SceneBatchRunner(scenes, f"http://127.0.0.1:{server.server_address[1]}")
    request_log = RequestLog(args.log_dir)
    server.RequestHandlerClass = make_handler(engines, server.server_address[1], cast, scenes, batch_runner, evaluations,
                                              request_log)
    request_log.start(APP_NAME, VERSION, port=server.server_address[1], device=device, state_root=state_root)
    for note in (*folder_notes, *evaluations.migration_notes):
        request_log.event(f"startup: {note}")
    print(f"Fel's Voice Lab on http://127.0.0.1:{server.server_address[1]}  (version {VERSION}, device: {device}, "
          f"free RAM {free_ram_gb():.1f} GB, log {request_log.path})", flush=True)
    if not args.no_cleanup:
        threading.Thread(target=run_startup_sweep, args=(OUT, request_log), kwargs={"keep": [request_log.path]},
                         name="startup-sweep", daemon=True).start()
    threading.Thread(target=measure_old_takes, args=(cast, request_log), name="loudness-backfill", daemon=True).start()
    if args.parent_pid:
        threading.Thread(target=exit_with_parent, args=(args.parent_pid, engines, server), daemon=True).start()
    keys = [k.strip() for k in args.preload.split(",") if k.strip() in engines.api_entries]
    if keys:
        threading.Thread(target=preload, args=(engines, keys), daemon=True).start()
    batch_runner.start()
    if args.desktop_session:
        from desktop_lifecycle import publish_session
        publish_session(args.desktop_session, server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        batch_runner.close(timeout=0)
        server.RequestHandlerClass.close_installer()
        engines.close()
        server.RequestHandlerClass.close_drama()
        batch_runner.close(timeout=10)
        server.server_close()
        request_log.event("stopped")
        request_log.close()


if __name__ == "__main__":
    main()
