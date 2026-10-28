"""Type-and-listen test page for Kokoro and Chatterbox.

    .venv\\Scripts\\python serve.py          then open http://127.0.0.1:8765

Local only (127.0.0.1). Models load the first time you use them and are unloaded after
--idle-minutes without a request, so the GPU is not held while other programs need it.
The /api/speak endpoint keeps a simple request and WAV response shape for external callers.
"""

import argparse
import base64
from datetime import datetime
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs as parse_drama_query

from common import LAB, OUT, free_ram_gb, pick_device, require_ram

import soundfile as sf

import api_spec
import housekeeping
import library_backup
import system_report
from app_version import APP_NAME, VERSION
from housekeeping import run_startup_sweep
from request_log import NullRequestLog, RequestLog, new_request_id
from runtime_folders import migrate_program_folders

from worker_pool import EnginePool, WorkerError
from engine_checks import EngineCheckStore
from cast_store import (CastStore, DIRECTION_ENGINES, MAX_UPLOAD_BYTES,
                        PRESET_VOICE_ENGINES, generation_identity_snapshot)
from scene_store import SceneBatchRunner, SceneStore, SUPPORTED_LANGUAGES, csv_template, json_template
import export_panel
import speaker_similarity
import transcript_suggest
from evaluation_store import EvaluationStore
from evaluation_cast import import_cast_takes
from evaluation_suites import list_suites
from ui_preferences import UiPreferences
from reference_catalog import list_references, list_voice_library, resolve_reference_audio, save_designed_reference
from kokoro_support import (discover_kokoro_voices, kokoro_languages,
                            normalize_kokoro_voices, phonemize_kokoro)
from chatterbox_support import CHATTERBOX_SAMPLING_FIELDS, normalize_chatterbox_sampling
from performance_settings import DEFAULTS, effective_settings, performance_controls
from language_matrix import unscreened_languages
from urllib.parse import quote

MIN_FREE_RAM_GB = {"kokoro": 8, "turbo": 10, "orig": 10, "breeze-tts-2": 12,
                   'drama-3':26,
                   "qwen3-tts-1.7b-voice-design": 10, "qwen3-tts-1.7b-base": 10,
                   "qwen3-tts-0.6b-base": 10, "qwen3-tts-1.7b-custom-voice": 10,
                   "voxcpm2": 10, "chatterbox-vc": 10}
LOCAL_HOSTS = ("127.0.0.1", "localhost")
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
PAGE_SCRIPTS = frozenset({"voice_lab_studio.js", "voice_lab_cast.js", "voice_lab_take_edit.js", "voice_lab_scenes.js",
                          "voice_lab_cast_bindings.js", "voice_lab_generate.js", "voice_lab_system.js", "voice_lab_app.js",
                          "voice_lab_drama.js"})
# The folder that holds desktop.local.json, which tells the desktop app which library to open. The tests point it elsewhere.
APP_ROOT = LAB
COMPARE_PAGE = _page("compare.html")
AUDIO_EXTENSIONS = {".wav", ".flac", ".ogg"}


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
    if cast is not None:
        cast.assert_media_usable(path)
    details = sf.info(path)
    maximum = 10 if target else 60
    if details.duration <= 0 or details.duration > maximum:
        raise ValueError(f"{role} audio must be non-empty and no longer than {maximum} seconds.")
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
    for root in roots:
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
            display_root = LAB if LAB in resolved.parents else cast.root
            row = {"path": str(resolved), "relative_path": resolved.relative_to(display_root).as_posix(),
                   "name": resolved.name, "source": root.relative_to(display_root).as_posix(),
                   "size_bytes": resolved.stat().st_size, "sample_rate_hz": details.samplerate,
                   "duration_seconds": round(float(details.duration), 3)}
            row.update(metadata_by_path.get(resolved, {}))
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


def make_handler(engines, port, cast_store=None, scene_store=None, batch_runner=None, evaluation_store=None,
                 request_log=None):
    log = request_log or NullRequestLog()
    cast = cast_store or CastStore()
    library_root = Path(getattr(cast, "root", LAB))
    preferences = UiPreferences(library_root)
    scenes = scene_store or SceneStore(cast, engines.entries)
    batches = batch_runner or SceneBatchRunner(scenes, f"http://127.0.0.1:{port}")
    # Scenes and Comparisons default to the Cast store's own library, so a handler built on a temporary Cast store cannot reach
    # the real one.
    evaluations = evaluation_store or EvaluationStore(lab_root=library_root)
    backups_folder = library_root / library_backup.BACKUP_FOLDER
    backup_task = library_backup.Task()

    def logged(kind, work, describe):
        """Wrap a backup, check or restore so the service log says how it ended: what was made and how big, or why it failed."""
        def run(progress):
            try:
                result = work(progress)
            except Exception as exc:
                log.event(f"{kind} failed: {type(exc).__name__}: {exc}")
                raise
            log.event(f"{kind} finished: {describe(result)}")
            return result
        return run

    drama_box=[]
    drama_lock=threading.Lock()
    def drama():
        with drama_lock:
            if not drama_box:
                from drama3_lab_studio import create
                drama_box.append(create(engines,cast))
            return drama_box[0]
    class Handler(BaseHTTPRequestHandler):
        _request_id = None
        _status = 0
        _sent_bytes = 0
        _notes = None

        @staticmethod
        def close_drama():
            if drama_box:drama_box[0].close()
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
                if self.command and log.wants(self.command, self._status, seconds):
                    try:
                        path = urlparse(self.path).path  # the query string can carry text, so it is never logged
                    except ValueError:
                        path = "-"
                    notes = dict(self._notes)
                    trace = notes.pop("traceback", None)
                    log.request(self._rid(), self.command, path, self._status, seconds,
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
            super().end_headers()

        def _local_only(self):
            """Refuse DNS-rebinding (a foreign Host header) and requests made by another website's page
            (a foreign Origin header). Callers that are not browsers, such as scripts, send no Origin."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in LOCAL_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed = urlparse(origin)
                    return (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
                            and (parsed.port or 80) == port)
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

        def _start_task(self, kind, label, work):
            """Run a backup, check or restore in the background; one at a time, and the page polls /api/system/task."""
            try:
                state = backup_task.start(kind, label, work)
            except library_backup.BackupBusy as exc:
                return self._json(409, {"error": str(exc)})
            return self._json(202, {"task": state})

        def _discard_body(self, length):
            """Read through a body that is being refused. Closing the connection with it unread resets the connection, and a
            client that is still sending then gets the reset and never reads the refusal. A body too big to read through, or
            sent too slowly, is left unread."""
            if length > MAX_DISCARD_BYTES:
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

        def _json_body(self, code, obj):
            return self._json(code, obj)

        def _stream_line(self, obj):
            payload = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()
            self._sent_bytes += len(payload)
            if obj.get("type") == "error":  # a stream that fails after its headers went out still answers 200
                self._note(error=obj.get("error"))

        def _perform(self, req):
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
            if direction.strip() and engine_id not in DIRECTION_ENGINES:
                raise ValueError(f"{engine_id} has no verified text-direction control; use its native event tags in the line text.")
            settings = req.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("Performance settings must be an object.")
            applied_settings = effective_settings(engine_id, settings)
            backend_request = {"engine": engine_id, "text": spoken_text}
            if identity["kind"] == "reference":
                reference = identity["reference"]
                backend_request["reference_path"] = reference["audio_path"]
                if engine_id in {"breeze-tts-2", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base"}:
                    backend_request["reference_text"] = reference["transcript"]
            if engine_id == "kokoro":
                backend_request.update(identity["engine_options"])
                backend_request["speed"] = settings.get("speed", 1.0)
                backend_request["language"] = language
            elif engine_id == "turbo":
                sampling = {key: settings[key] for key in CHATTERBOX_SAMPLING_FIELDS[engine_id] if key in settings}
                backend_request.update({"clone": True, **normalize_chatterbox_sampling(engine_id, sampling)})
            elif engine_id == "orig":
                sampling = {key: settings[key] for key in CHATTERBOX_SAMPLING_FIELDS[engine_id] if key in settings}
                sampling.setdefault("exaggeration", 0.6)
                sampling.setdefault("cfg_weight", 0.4)
                backend_request.update({"clone": True, **normalize_chatterbox_sampling(engine_id, sampling)})
            elif engine_id == "breeze-tts-2":
                backend_request.update({"mode": "direction" if direction.strip() else "clone",
                                        "profile": settings.get("profile", "eager"),
                                        "direction": direction, "seed": settings.get("seed", 42),
                                        "cfg_scale": settings.get("cfg_scale", 1.0),
                                        "max_new_tokens": settings.get("max_new_tokens", 1500),
                                        "temperature": settings.get("temperature", 0.9),
                                        "top_k": settings.get("top_k", 50), "top_p": settings.get("top_p", 1.0),
                                        "repetition_penalty": settings.get("repetition_penalty", 1.1),
                                        "depth_temperature": settings.get("depth_temperature", 0.9),
                                        "depth_top_k": settings.get("depth_top_k", 50),
                                        "depth_top_p": settings.get("depth_top_p", 1.0)})
            elif engine_id in {"qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base"}:
                if not isinstance(language, str) or not language.strip():
                    raise ValueError("Choose a language for Qwen Base performance.")
                backend_request.update({"mode": "clone", "language": language,
                                        "direction": direction, "seed": settings.get("seed", 42),
                                        "max_new_tokens": settings.get("max_new_tokens", 512),
                                        "temperature": settings.get("temperature", 0.9),
                                        "top_k": settings.get("top_k", 50), "top_p": settings.get("top_p", 1.0),
                                        "repetition_penalty": settings.get("repetition_penalty", 1.05),
                                        "xvec_only": False})
            elif engine_id == "qwen3-tts-1.7b-custom-voice":
                options = identity["engine_options"]
                backend_request.update({"mode": "custom_voice", "speaker": options["speaker"],
                                        "language": language, "direction": direction,
                                        "seed": settings.get("seed", 42),
                                        "max_new_tokens": settings.get("max_new_tokens", 512),
                                        "temperature": settings.get("temperature", 0.9),
                                        "top_k": settings.get("top_k", 50), "top_p": settings.get("top_p", 1.0),
                                        "repetition_penalty": settings.get("repetition_penalty", 1.05)})
            elif engine_id == 'drama-3':
                backend_request.update(mode='clone', reference_text=identity['reference']['transcript'],
                                       direction=direction, language=language, **applied_settings)
            elif engine_id == "voxcpm2":
                backend_request.update({
                    "mode": "clone", "direction": direction,
                    "seed": settings.get("seed", 42),
                    "cfg_value": settings.get("cfg_value", 2.0),
                    "inference_timesteps": settings.get("inference_timesteps", 10),
                })
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
            if engine_id == "breeze-tts-2":
                generation_mode = backend_request["mode"]
            elif engine_id in PRESET_VOICE_ENGINES:
                generation_mode = "custom_voice"
            elif engine_id.startswith("qwen3-tts"):
                generation_mode = "clone"
            elif engine_id in {"voxcpm2", "drama-3"}:
                generation_mode = backend_request["mode"]
            elif engine_id in {"turbo", "orig"}:
                generation_mode = "clone"
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
            if engine_id == "kokoro":
                model["fixed_voice"] = voice_options.get("voice", "af_heart")
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

        def do_GET(self):
            if not self._local_only():
                return self._json(403, {"error": "local requests only"})
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/theme.js':
                return self._send(200, (LAB / 'theme.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/voice_workflows.js':
                return self._send(200, (LAB / 'voice_workflows.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/voice_library.js':
                return self._send(200, (LAB / 'voice_library.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/api/preferences':
                return self._json(200, preferences.get())
            if path == '/drama3-studio.js':
                return self._send(200,(LAB/'drama3_studio.js').read_bytes(),'text/javascript; charset=utf-8')
            if path.lstrip('/') in PAGE_SCRIPTS:
                return self._send(200, (LAB / path.lstrip('/')).read_bytes(), 'text/javascript; charset=utf-8')
            if path.startswith('/api/drama3/'):
                try:
                    from drama3_production import capabilities
                    studio=drama();suffix=path[len('/api/drama3/'):]
                    if suffix=='info':return self._json(200,{**capabilities(),'sources':studio.sources(),'jobs':studio.list()})
                    match=re.fullmatch(r'jobs/([a-f0-9]{32})(?:/(audio|metadata|package))?',suffix)
                    if not match:return self._json(404,{'error':'Drama endpoint not found'})
                    if match.group(2):
                        data,ctype=studio.artifact(*match.groups())
                        return self._send(200,data,ctype,{'Content-Disposition':'inline' if match.group(2)=='audio' else 'attachment; filename=drama-take.'+('zip' if match.group(2)=='package' else 'json')})
                    return self._json(200,studio.get(match.group(1),int(parse_drama_query(parsed.query).get('offset',['0'])[0])))
                except KeyError as exc:return self._json(404,{'error':str(exc)})
                except (ValueError,RuntimeError) as exc:return self._json(400,{'error':str(exc)})
            if path in ("/", "/index.html"):
                self._send(200, preferences.render(PAGE), "text/html; charset=utf-8")
            elif path in ("/compare", "/compare.html"):
                self._send(200, preferences.render(COMPARE_PAGE), "text/html; charset=utf-8")
            elif path == "/api/info":
                self._json(200, {"device": engines.device, "loaded": engines.loaded,
                                 "voices": discover_kokoro_voices(), "kokoro_languages": kokoro_languages(),
                                 "free_ram_gb": round(free_ram_gb(), 1)})
            elif path == "/api/catalog":
                self._json(200, engines.catalog)
            elif path == "/api/performance/settings":
                schemas = []
                for key, entry in engines.api_entries.items():
                    if key in DEFAULTS:
                        schemas.append(performance_schema(key, entry))
                self._json(200, {"engines": schemas, "unscreened_languages": unscreened_by_engine(engines.api_entries)})
            elif path == "/api/evaluations/suites":
                self._json(200, list_suites())
            elif path == "/api/evaluations":
                self._json(200, {"runs": evaluations.list_runs()})
            elif path == "/api/evaluations/blind-sessions":
                self._json(200, {"sessions": evaluations.list_blind_sessions()})
            elif re.fullmatch(r"/api/evaluations/runs/[A-Za-z0-9_-]{1,80}", path):
                try:
                    self._json(200, {"run": evaluations.get_run(path.rsplit("/", 1)[1])})
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/evaluations/blind-sessions/[a-f0-9]{32}", path):
                try:
                    self._json(200, {"session": evaluations.get_blind_session(path.rsplit("/", 1)[1])})
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/evaluations/blind-audio/[a-f0-9]{32}", path):
                try:
                    self._send(200, evaluations.get_blind_audio_path(path.rsplit("/", 1)[1]).read_bytes(), "audio/wav")
                except (KeyError, FileNotFoundError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/evaluations/items/[a-f0-9]{32}", path):
                try:
                    self._json(200, {"item": evaluations.get_item(path.rsplit("/", 1)[1])})
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
            elif path == "/api/cast":
                self._json(200, {"schema_version": 1, "characters": cast.list_cast()})
            elif path == "/api/scenes":
                self._json(200, {"schema_version": 1, "scenes": scenes.list_scenes()})
            elif path == "/api/exports":
                from urllib.parse import parse_qs
                # ?recheck=1 asks FFmpeg again, for a page whose user has just installed it.
                recheck = parse_qs(parsed.query).get("recheck") == ["1"]
                self._json(200, {"exports": export_panel.list_exports(scenes.root),
                                 "ffmpeg": export_panel.ffmpeg_report(refresh=recheck)})
            elif path == "/api/system":
                from urllib.parse import parse_qs
                recheck = parse_qs(parsed.query).get("recheck") == ["1"]
                report = system_report.build(
                    app=APP_NAME, version=VERSION, state_root=library_root, out_dir=OUT, device=engines.device,
                    ffmpeg=export_panel.ffmpeg_report(refresh=recheck), engine_status=engines.engine_status(),
                    log_path=getattr(log, "path", None), free_ram=free_ram_gb())
                report["diagnostics"] = system_report.diagnostics_text(report)
                self._json(200, report)
            elif path == "/api/system/storage":
                self._json(200, system_report.storage(library_root, OUT))
            elif path == "/api/system/backups":
                self._json(200, {"folder": str(backups_folder), "backups": library_backup.list_backups(backups_folder),
                                 "plan": library_backup.plan(library_root), "task": backup_task.status(),
                                 "suggested_folder": library_backup.suggested_folder(library_root)})
            elif path == "/api/system/task":
                self._json(200, {"task": backup_task.status()})
            elif re.fullmatch(r"/api/takes/[a-f0-9]{32}/voice-match", path):
                try:
                    self._json(200, {"voice_match": speaker_similarity.voice_match(cast, path.split("/")[3])})
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
                except FileNotFoundError as exc:
                    self._json(503, {"error": str(exc)})
            elif path == "/api/scenes/template.json":
                self._json(200, json_template())
            elif path == "/api/scenes/template.csv":
                self._send(200, csv_template(), "text/csv; charset=utf-8",
                           {"Content-Disposition": "attachment; filename=voice-lab-scene-template.csv"})
            elif re.fullmatch(r"/api/scenes/[a-f0-9]{32}", path):
                try:
                    self._json(200, {"scene": scenes.get_scene(path.rsplit("/", 1)[1])})
                except (ValueError, KeyError, RuntimeError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/scenes/[a-f0-9]{32}/export\.json", path):
                scene_id = path.split("/")[3]
                try:
                    self._send(200, scenes.export_json(scene_id), "application/json; charset=utf-8",
                               {"Content-Disposition": f"attachment; filename=scene-{scene_id}.json"})
                except (ValueError, KeyError, RuntimeError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/scenes/[a-f0-9]{32}/export\.csv", path):
                scene_id = path.split("/")[3]
                try:
                    self._send(200, scenes.export_csv(scene_id), "text/csv; charset=utf-8",
                               {"Content-Disposition": f"attachment; filename=scene-{scene_id}.csv"})
                except (ValueError, KeyError, RuntimeError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/scenes/[a-f0-9]{32}/previews/[A-Za-z0-9._-]+\.wav", path):
                # Preview audio lives in the scene store's own output folder, which need not be the project's out/.
                folder = (scenes.output_root / path.split("/")[3]).resolve()
                target = (folder / path.rsplit("/", 1)[1]).resolve()
                if target.parent == folder and target.is_file():
                    self._send(200, target.read_bytes(), "audio/wav")
                else:
                    self._json(404, {"error": "Scene preview not found."})
            elif re.fullmatch(r"/api/scenes/[a-f0-9]{32}/segments", path):
                from urllib.parse import parse_qs
                scene_id = path.split("/")[3]
                line_id = (parse_qs(parsed.query).get("line_id") or [None])[0]
                try:
                    self._json(200, {"segments": scenes.segment_preview(scene_id, line_id)})
                except (ValueError, KeyError, RuntimeError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/scenes/[a-f0-9]{32}/jobs", path):
                scene_id = path.split("/")[3]
                try:
                    self._json(200, {"jobs": scenes.list_jobs(scene_id)})
                except (ValueError, KeyError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/scenes/jobs/[a-f0-9]{32}", path):
                try:
                    self._json(200, {"job": scenes.get_job(path.rsplit("/", 1)[1])})
                except (ValueError, KeyError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/scenes/jobs/[a-f0-9]{32}/manifest\.json", path):
                job_id = path.split("/")[4]
                try:
                    valid, info = scenes.verify_job_manifest(job_id)
                    if not valid:
                        self._json(409, {"error": "Batch manifest checksum mismatch.", "details": info})
                    else:
                        self._send(200, Path(info["path"]).read_bytes(), "application/json; charset=utf-8")
                except (ValueError, KeyError) as exc:
                    self._json(404, {"error": str(exc)})
            elif path == "/api/pronunciations":
                from urllib.parse import parse_qs
                params = parse_qs(parsed.query)
                character_id = (params.get("character_id") or [None])[0]
                self._json(200, {"pronunciations": cast.list_pronunciations(character_id)})
            elif path == "/api/takes":
                from urllib.parse import parse_qs
                params = parse_qs(parsed.query)
                identity_id = (params.get("identity_id") or [None])[0]
                line_id = (params.get("line_id") or [None])[0]
                self._json(200, {"takes": cast.list_takes(identity_id, line_id)})
            elif re.fullmatch(r"/api/takes/[a-f0-9]{32}", path):
                try:
                    self._json(200, {"take": cast.get_take(path.split("/")[3])})
                except (ValueError, KeyError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/takes/[a-f0-9]{32}/waveform", path):
                params = parse_drama_query(parsed.query)
                try:
                    self._json(200, cast.take_waveform(path.split("/")[3], (params.get("points") or ["600"])[0]))
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/references":
                self._json(200, {"references": list_references(LAB, cast)})
            elif path == "/api/voice-library":
                params = parse_drama_query(parsed.query)
                self._json(200, list_voice_library(LAB, cast, include_retired=(params.get("include_retired") or [""])[0] == "1"))
            elif path == "/api/purges/preview":
                params = parse_drama_query(parsed.query)
                try:
                    self._json(200, cast.purge_preview((params.get("kind") or [None])[0], (params.get("id") or [None])[0]))
                except KeyError as exc:
                    self._json(404, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/reference-audio":
                try:
                    params = parse_drama_query(parsed.query)
                    requested = (params.get("path") or [None])[0]
                    target, content_type = resolve_reference_audio(requested, LAB, cast)
                    self._send(200, target.read_bytes(), content_type)
                except (OSError, RuntimeError, ValueError):
                    self._json(404, {"error": "Reference audio is unavailable or outside this Lab's reference storage."})
            elif path == "/api/conversion/sources":
                self._json(200, {"sources": _conversion_asset_rows(cast, target=False)})
            elif path == "/api/conversion/targets":
                self._json(200, {"targets": _conversion_asset_rows(cast, target=True)})
            elif re.fullmatch(r"/api/references/[a-f0-9]{32}/audio", path):
                ref_id = path.split("/")[3]
                try:
                    target = cast.get_media_path("reference", ref_id)
                    self._send(200, target.read_bytes(), "audio/wav", {"X-Reference-Version": ref_id})
                except (ValueError, KeyError, FileNotFoundError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/references/[a-f0-9]{32}/source", path):
                ref_id = path.split("/")[3]
                try:
                    target = cast.get_media_path("reference_source", ref_id)
                    content_type = {".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
                                    ".m4a": "audio/mp4"}.get(target.suffix.lower(), "application/octet-stream")
                    self._send(200, target.read_bytes(), content_type, {"X-Reference-Version": ref_id})
                except (ValueError, KeyError, FileNotFoundError) as exc:
                    self._json(404, {"error": str(exc)})
            elif re.fullmatch(r"/api/takes/[a-f0-9]{32}/audio", path):
                take_id = path.split("/")[3]
                try:
                    target = cast.get_media_path("take", take_id)
                    self._send(200, target.read_bytes(), "audio/wav", {"X-Take-Id": take_id})
                except (ValueError, KeyError, FileNotFoundError) as exc:
                    self._json(404, {"error": str(exc)})
            elif path == "/api/health":
                self._json(200, {**engines.health(), "version": VERSION})
            elif path == "/api/openapi.json":
                self._json(200, api_spec.build_openapi())
            elif path == "/api/engines/status":
                self._json(200, engines.engine_status())
            elif path.startswith("/out/"):  # files the Lab wrote to its out folder, such as a converted voice
                name = path[5:]
                target = (OUT / name).resolve()
                ctype = {".wav": "audio/wav", ".json": "application/json"}.get(target.suffix)
                if ctype and OUT.resolve() in target.parents and target.is_file():
                    self._send(200, target.read_bytes(), ctype)
                else:
                    self._json(404, {"error": "not found"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_only():
                return self._json(403, {"error": "local requests only"})
            parsed = urlparse(self.path)
            path = parsed.path
            preview = re.fullmatch(r"/api/identities/([a-f0-9]{32})/references/preview", path)
            if preview:
                try:
                    body = self._read_body(MAX_UPLOAD_BYTES)
                    encoded = self.headers.get("X-Reference-Metadata", "")
                    metadata = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                    if not isinstance(metadata, dict):
                        raise ValueError("Reference metadata must be an object.")
                    audio, metrics = cast.preview_reference(
                        preview.group(1), metadata.get("filename"), body, edits=metadata.get("edits"))
                    quality = json.dumps(metrics["quality"], separators=(",", ":")).encode("ascii")
                    headers = {"X-Reference-Duration": f"{metrics['duration_seconds']:.3f}",
                               "X-Reference-Sample-Rate": str(metrics["sample_rate"]),
                               "X-Reference-RMS": f"{metrics['rms_dbfs']:.1f}",
                               "X-Reference-Quality": base64.urlsafe_b64encode(quality).decode("ascii")}
                    return self._send(200, audio, "audio/wav", headers)
                except OverflowError as exc:
                    return self._json(413, {"error": str(exc)})
                except KeyError as exc:
                    return self._json(404, {"error": str(exc)})
                except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
            upload = re.fullmatch(r"/api/identities/([a-f0-9]{32})/references", path)
            if upload:
                try:
                    body = self._read_body(MAX_UPLOAD_BYTES)
                    encoded = self.headers.get("X-Reference-Metadata", "")
                    metadata = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                    if not isinstance(metadata, dict):
                        raise ValueError("Reference metadata must be an object.")
                    saved = cast.add_reference(upload.group(1), metadata.get("filename"), body,
                                               transcript=metadata.get("transcript", ""),
                                               origin=metadata.get("origin", ""),
                                               allowed_use=metadata.get("allowed_use", ""),
                                               edits=metadata.get("edits"))
                    return self._json(201, {"reference": saved})
                except OverflowError as exc:
                    return self._json(413, {"error": str(exc)})
                except KeyError as exc:
                    return self._json(404, {"error": str(exc)})
                except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
            try:
                scene_request = path == "/api/scenes" or path == "/api/scenes/import" or path.startswith("/api/scenes/")
                req = self._read_json(MAX_SCENE_REQUEST_BYTES if scene_request else MAX_REQUEST_BYTES)
                if path == "/api/preferences":
                    try:
                        return self._json(200, preferences.save(req))
                    except OSError:
                        return self._json(500, {"error": "Could not save appearance preferences."})
                if path == "/api/exports/preview":
                    return self._json(200, export_panel.run(scenes.root, req, dry_run=True))
                if path == "/api/exports":
                    try:
                        return self._json(201, export_panel.run(scenes.root, req))
                    except export_panel.ExportBusy as exc:
                        return self._json(409, {"error": str(exc)})
                export_verify = re.fullmatch(r"/api/exports/([A-Za-z0-9][A-Za-z0-9._-]{0,89})/verify", path)
                if export_verify:
                    return self._json(200, export_panel.verify(scenes.root, export_verify.group(1)))
                transcript_draft = re.fullmatch(r"/api/references/([a-f0-9]{32})/transcript-suggestion", path)
                if transcript_draft:
                    # A draft only: nothing is saved or approved until the user checks and saves the text.
                    try:
                        audio = cast.get_media_path("reference", transcript_draft.group(1))
                        return self._json(200, {"suggestion": transcript_suggest.suggest(audio)})
                    except KeyError as exc:
                        return self._json(404, {"error": str(exc)})
                    except FileNotFoundError as exc:
                        return self._json(404, {"error": str(exc)})
                    except transcript_suggest.TranscriptBusy as exc:
                        return self._json(409, {"error": str(exc)})
                    except transcript_suggest.TranscriptUnavailable as exc:
                        return self._json(503, {"error": str(exc)})
                    except RuntimeError as exc:
                        return self._json(500, {"error": str(exc)})
                if path == "/api/system/backups":
                    include = req.get("include", list(library_backup.OPTIONAL))
                    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
                        raise ValueError("include must be a list of section names.")
                    library_backup.chosen_sections(include)  # an unknown name is refused now, not after the thread starts
                    return self._start_task("backup", "Backing up the library", logged(
                        "backup", lambda progress: library_backup.create(
                            library_root, backups_folder, include=include, app=APP_NAME, version=VERSION, progress=progress),
                        lambda result: f"{result['name']}, {result['files']} files, {result['bytes']} bytes"))
                if path == "/api/system/backups/verify":
                    source = library_backup.backup_path(backups_folder, req.get("name"))
                    return self._start_task("check", f"Checking {source.name}", logged(
                        "check", lambda progress: library_backup.verify(source, progress=progress),
                        lambda result: f"{result['name']} {'passed' if result['ok'] else 'did not pass'}"))
                if path == "/api/system/backups/restore":
                    source = library_backup.backup_path(backups_folder, req.get("name"))
                    target = library_backup.restore_target(library_root, req.get("folder"))
                    return self._start_task("restore", f"Restoring {source.name}", logged(
                        "restore", lambda progress: {**library_backup.restore(source, target, progress=progress),
                                                     "folder": target.name},
                        lambda result: f"{result['folder']}, {result['files']} files"))
                if path == "/api/system/use-library":
                    return self._json(200, library_backup.use_library(
                        APP_ROOT, library_backup.restore_target(library_root, req.get("folder"))))
                if path == "/api/system/cleanup":
                    log_file = getattr(log, "path", None)
                    result = housekeeping.apply(housekeeping.plan(OUT, keep=[log_file] if log_file else []), OUT)
                    log.event(f"cleanup on request removed {result['removed']} items ({result['bytes']} bytes)"
                              + (f"; {len(result['errors'])} could not be removed" if result["errors"] else ""))
                    return self._json(200, result)
                if path == '/api/drama3/jobs':
                    return self._json(202,{'job':drama().start(req)})
                drama_cancel=re.fullmatch(r'/api/drama3/jobs/([a-f0-9]{32})/cancel',path)
                if drama_cancel:return self._json(200,{'job':drama().cancel(drama_cancel.group(1))})
            except OverflowError as exc:
                return self._json(413, {"error": str(exc)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})

            try:
                if path == "/api/convert":
                    source_path, source_info = _conversion_audio_path(
                        req.get("source_path"), "Source performance", target=False, cast=cast)
                    target_path, target_info = _conversion_audio_path(
                        req.get("target_reference_path"), "Target voice reference", target=True, cast=cast)
                    if source_path == target_path:
                        raise ValueError("Source performance and target voice reference must be separate files.")
                    target_metadata = {}
                    reference_id = req.get("target_reference_id")
                    if reference_id:
                        reference = cast.get_reference(reference_id)
                        recorded_target = Path(cast.get_media_path("reference", reference_id)).resolve()
                        if recorded_target != target_path:
                            raise ValueError("Target reference path does not match the selected Cast reference version.")
                        target_metadata = {
                            "reference_version_id": reference["id"],
                            "origin": reference.get("origin", ""),
                            "allowed_use": reference.get("allowed_use", ""),
                        }
                    else:
                        target_metadata = next((row for row in _conversion_asset_rows(cast, target=True)
                                                if Path(row["path"]).resolve() == target_path), {})
                    wav, sample_rate, engine_info = engines.convert(str(source_path), str(target_path))
                    import numpy as np
                    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
                    if not audio.size or not np.isfinite(audio).all():
                        raise RuntimeError("Chatterbox voice conversion produced empty or invalid audio.")
                    conversion_id = uuid.uuid4().hex
                    artifact_dir = OUT / "conversions" / conversion_id
                    artifact_dir.mkdir(parents=True, exist_ok=False)
                    audio_path = artifact_dir / "converted.wav"
                    manifest_path = artifact_dir / "manifest.json"
                    wav_buf = io.BytesIO()
                    sf.write(wav_buf, audio, int(sample_rate), format="WAV", subtype="PCM_16")
                    wav_bytes = wav_buf.getvalue()
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
                                                     if Path(row["path"]).resolve() == source_path), None)})
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
                    return self._json(201, {"conversion": {
                        "conversion_id": conversion_id, "audio_url": audio_url,
                        "audio_path": str(audio_path.resolve()), "manifest_path": str(manifest_path.resolve()),
                        "manifest": manifest,
                    }})
                if path == "/api/kokoro/phonemize":
                    return self._json(200, phonemize_kokoro(req.get("text", ""), req.get("language", "English")))
                if path == "/api/scenes":
                    return self._json(201, {"scene": scenes.create_scene(req)})
                if path == "/api/evaluations/runs/import":
                    return self._json(201, {"run": evaluations.register_run_manifest(req.get("manifest_path", ""))})
                if path == "/api/evaluations/cast-import":
                    return self._json(201, {"run": import_cast_takes(
                        cast, evaluations, req.get("take_ids"), title=req.get("title", "Saved voice comparison"))})
                if path == "/api/evaluations/blind-sessions":
                    return self._json(201, {"session": evaluations.create_blind_session(
                        req.get("run_id"), req.get("evaluator", "local listener"))})
                blind_rating = re.fullmatch(r"/api/evaluations/blind-sessions/([a-f0-9]{32})/ratings", path)
                if blind_rating:
                    return self._json(200, {"session": evaluations.save_rating(
                        blind_rating.group(1), req.get("trial_id"), req.get("ratings"), req.get("notes", ""))})
                blind_reveal = re.fullmatch(r"/api/evaluations/blind-sessions/([a-f0-9]{32})/reveal", path)
                if blind_reveal:
                    return self._json(200, {"session": evaluations.reveal_blind_session(blind_reveal.group(1))})
                transcript = re.fullmatch(r"/api/evaluations/items/([a-f0-9]{32})/transcript", path)
                if transcript:
                    return self._json(200, evaluations.add_transcript(
                        transcript.group(1), req.get("heard_text"), req.get("evaluator")))
                if path == "/api/scenes/import":
                    fmt = req.get("format")
                    content = req.get("content")
                    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_SCENE_REQUEST_BYTES:
                        raise ValueError("Scene import content must be UTF-8 text up to 2 MiB.")
                    if fmt == "json":
                        try:
                            manifest = json.loads(content)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"Invalid scene JSON: {exc}") from exc
                    elif fmt == "csv":
                        manifest = scenes.import_csv(content.encode("utf-8"))
                    else:
                        raise ValueError("Scene import format must be json or csv.")
                    return self._json(201, {"scene": scenes.create_scene(manifest)})
                scene_update = re.fullmatch(r"/api/scenes/([a-f0-9]{32})", path)
                if scene_update:
                    return self._json(200, {"scene": scenes.update_scene(
                        scene_update.group(1), req, expected_revision=req.get("expected_revision"))})
                scene_job = re.fullmatch(r"/api/scenes/([a-f0-9]{32})/jobs", path)
                if scene_job:
                    job = scenes.create_job(scene_job.group(1), req.get("line_ids"), req.get("segment_ids"))
                    batches.wake()
                    return self._json(202, {"job": job})
                scene_select = re.fullmatch(r"/api/scenes/([a-f0-9]{32})/select", path)
                if scene_select:
                    return self._json(200, {"scene": scenes.select_take(scene_select.group(1), req.get("line_id"),
                        req.get("take_id"), req.get("segment_id"))})
                scene_preview = re.fullmatch(r"/api/scenes/([a-f0-9]{32})/preview", path)
                if scene_preview:
                    preview = scenes.assemble_preview(scene_preview.group(1))
                    preview_url = f"/api/scenes/{scene_preview.group(1)}/previews/"
                    preview["audio_url"] = preview_url + Path(preview["audio_path"]).name
                    preview["mastered_audio_url"] = preview_url + Path(preview["mastered_audio_path"]).name
                    return self._json(201, {"preview": preview})
                scene_action = re.fullmatch(r"/api/scenes/jobs/([a-f0-9]{32})/(pause|resume|cancel|retry)", path)
                if scene_action:
                    job_id, action = scene_action.groups()
                    if action == "pause":
                        job = scenes.request_pause(job_id)
                    elif action == "resume":
                        job = scenes.resume(job_id)
                    elif action == "cancel":
                        job = scenes.cancel(job_id)
                    else:
                        job = scenes.retry_failed(job_id)
                    batches.wake()
                    return self._json(200, {"job": job})
                character = re.fullmatch(r"/api/characters/([a-f0-9]{32})", path)
                identities = re.fullmatch(r"/api/characters/([a-f0-9]{32})/identities", path)
                identity_update = re.fullmatch(r"/api/identities/([a-f0-9]{32})/update", path)
                ref_version = re.fullmatch(r"/api/references/([a-f0-9]{32})/versions", path)
                from_take = re.fullmatch(r"/api/identities/([a-f0-9]{32})/references/from-take", path)
                pronunciation = path == "/api/pronunciations"
                take_update = re.fullmatch(r"/api/takes/([a-f0-9]{32})/update", path)
                take_edit_save = re.fullmatch(r"/api/takes/([a-f0-9]{32})/edit", path)
                take_edit_preview = re.fullmatch(r"/api/takes/([a-f0-9]{32})/edit-preview", path)
                if path == "/api/characters":
                    return self._json(201, {"character": cast.create_character(req.get("name"), req.get("description", ""), req.get("tags"))})
                if character:
                    return self._json(200, {"character": cast.update_character(character.group(1), req)})
                if identities:
                    engine_id = req.get("engine_id")
                    entry = engines.api_entries.get(engine_id)
                    if entry is None:
                        raise ValueError("Choose an engine exposed by the local service.")
                    capabilities = entry.get("capabilities", {})
                    clone_status = capabilities.get("voice_cloning", {}).get("status")
                    fixed_voice = capabilities.get("voice_selection", {}).get("status") == "supported"
                    preset_voice = (engine_id in PRESET_VOICE_ENGINES and
                                    capabilities.get("voice_selection", {}).get("status") in {"supported", "worker_verified"})
                    if clone_status not in {"supported", "worker_verified"} and not (engine_id == "kokoro" and fixed_voice) and not preset_voice:
                        raise ValueError("This model does not provide a reusable clone reference or a fixed voice selector.")
                    if engine_id == "kokoro":
                        options = {"voice": ",".join(normalize_kokoro_voices(
                            req.get("voice", "af_heart"), discover_kokoro_voices()))}
                    elif preset_voice:
                        loaded_info = getattr(engines, "loaded_model_info", lambda _key: None)(engine_id)
                        if not loaded_info:
                            raise ValueError("Load the CustomVoice checkpoint first so its local speakers and languages can be verified.")
                        supported_speakers = loaded_info.get("supported_speakers") or []
                        supported_languages = loaded_info.get("supported_languages") or []
                        speaker = req.get("speaker")
                        language = req.get("language")
                        if not isinstance(speaker, str) or speaker not in supported_speakers:
                            raise ValueError("Choose a speaker reported by the loaded CustomVoice checkpoint.")
                        language_match = next((item for item in supported_languages
                                               if isinstance(language, str) and item.casefold() == language.casefold()), None)
                        if language_match is None:
                            raise ValueError("Choose a language reported by the loaded CustomVoice checkpoint.")
                        default_direction = req.get("default_direction", "")
                        if not isinstance(default_direction, str) or len(default_direction) > 1000:
                            raise ValueError("Preset delivery instruction must be text of 1000 characters or fewer.")
                        controls = performance_controls(engine_id, capabilities)
                        checkpoint = loaded_info.get("checkpoint") or entry["checkpoint"]
                        checkpoint_revision = loaded_info.get("checkpoint_revision") or entry["local_revision"]
                        if checkpoint != entry["checkpoint"] or checkpoint_revision != entry["local_revision"]:
                            raise ValueError("The loaded CustomVoice model does not match the catalog's pinned checkpoint revision.")
                        options = {
                            "checkpoint": checkpoint,
                            "checkpoint_revision": checkpoint_revision,
                            "speaker": speaker, "language": language_match,
                            "supported_speakers": list(supported_speakers),
                            "supported_languages": list(supported_languages),
                            "default_direction": default_direction.strip(),
                            "settings_schema": controls["controls"],
                            "capability_snapshot": {"voice_direction": controls["direction"],
                                "sampling_controls": capabilities.get("sampling_controls", {}),
                                "acting_controls": capabilities.get("acting_controls", [])},
                        }
                    else:
                        options = {}
                    return self._json(201, {"identity": cast.create_identity(identities.group(1), engine_id,
                                                                              req.get("label"), options,
                                                                              req.get("notes", ""))})
                if identity_update:
                    return self._json(200, {"identity": cast.update_identity(
                        identity_update.group(1), req, fixed_voices=discover_kokoro_voices())})
                if ref_version:
                    return self._json(201, {"reference": cast.add_reference_version(ref_version.group(1), req)})
                if from_take:
                    return self._json(201, {"reference": cast.add_reference_from_take(
                        from_take.group(1), req.get("take_id"), transcript=req.get("transcript"),
                        edits=req.get("edits"), origin=req.get("origin"), allowed_use=req.get("allowed_use"))})
                if pronunciation:
                    return self._json(200, {"pronunciation": cast.save_pronunciation(
                        source=req.get("source"), replacement=req.get("replacement"),
                        character_id=req.get("character_id"), notes=req.get("notes", ""))})
                if take_update:
                    return self._json(200, {"take": cast.set_take_state(take_update.group(1),
                        status=req.get("status"), rating=req.get("rating"), notes=req.get("notes"))})
                if take_edit_save:
                    edit_key = req.pop("idempotency_key", None)
                    take, replayed = cast.create_edited_take(take_edit_save.group(1), req, request_key=edit_key)
                    return self._json(200 if replayed else 201, {"take": take, "replayed": replayed})
                if take_edit_preview:
                    audio, report = cast.preview_edit(take_edit_preview.group(1), req)
                    return self._send(200, audio, "audio/wav", {
                        "X-Audio-Seconds": f"{report['duration_seconds']:.2f}",
                        "X-Edit-Start-Seconds": f"{report['start_seconds']:.3f}",
                        "X-Edit-End-Seconds": f"{report['end_seconds']:.3f}",
                        "X-Peak-Dbfs": "" if report["peak_dbfs"] is None else f"{report['peak_dbfs']:.2f}"})
                if path == "/api/retirements":
                    return self._json(201, cast.retire(req.get("kind"), req.get("id"), reason=req.get("reason", "")))
                if path == "/api/retirements/restore":
                    return self._json(200, cast.restore(req.get("kind"), req.get("id")))
                if path == "/api/purges":
                    return self._json(200, cast.purge(req.get("kind"), req.get("id"), reason=req.get("reason", ""),
                                                      confirm=req.get("confirm")))
                engine_check = re.fullmatch(r"/api/engines/([a-z0-9][a-z0-9.-]{0,80})/check", path)
                if engine_check:
                    return self._json(200, {"check": engines.check(engine_check.group(1))})
                if path == "/api/performance":
                    return self._perform(req)
            except KeyError as exc:
                return self._json(404, {"error": str(exc)})
            except (ValueError, MemoryError) as exc:
                return self._json(400, {"error": str(exc)})
            except WorkerError as exc:
                return self._json(exc.status_code, {"error": str(exc)})
            except Exception as exc:
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            if self.path == "/api/unload":
                try:
                    engines.unload()
                    return self._json(200, {"ok": True})
                except WorkerError as exc:
                    return self._json(exc.status_code, {"error": str(exc)})
            if self.path == "/api/load":
                try:
                    return self._json(200, engines.load(req.get("engine", "kokoro"), profile=req.get("profile")))
                except (ValueError, MemoryError) as exc:
                    return self._json(400, {"error": str(exc)})
                except WorkerError as exc:
                    return self._json(exc.status_code, {"error": str(exc)})
            if self.path == "/api/prepare-reference":
                try:
                    result = engines.prepare_reference(req.get("engine"), req.get("path"),
                                                       exaggeration=req.get("exaggeration", 0.5),
                                                       profile=req.get("profile"),
                                                       reference_text=req.get("reference_text", ""),
                                                       xvec_only=req.get("xvec_only", False))
                    return self._json(200, result)
                except (ValueError, MemoryError) as exc:
                    return self._json(400, {"error": str(exc)})
                except WorkerError as exc:
                    return self._json(exc.status_code, {"error": str(exc)})
            if path == "/api/stream":
                started = time.perf_counter()
                self._note(engine=req.get("engine"))
                save_path = None
                save_name = req.get("save_voice_as")
                if save_name not in (None, ""):
                    if req.get("engine") != "qwen3-tts-1.7b-voice-design" or req.get("mode", "design") != "design":
                        return self._json(400, {"error": "Only a Qwen VoiceDesign stream can be saved as a reusable voice reference."})
                    if not isinstance(save_name, str) or not VOICE_ASSET_NAME_RE.fullmatch(save_name):
                        return self._json(400, {"error": "save_voice_as must be a safe name with up to two folders; use letters, digits, underscores, or hyphens."})
                    asset_root = LAB / "data" / "voices"
                    asset_root.mkdir(parents=True, exist_ok=True)
                    resolved_root = asset_root.resolve()
                    if LAB.resolve() not in resolved_root.parents:
                        return self._json(400, {"error": "Voice asset storage must remain inside voice-lab/data/voices/."})
                    save_path = resolved_root.joinpath(*save_name.split("/")).with_suffix(".wav")
                    if resolved_root not in save_path.resolve().parents:
                        return self._json(400, {"error": "Voice asset path must remain inside voice-lab/data/voices/."})
                    if save_path.exists() or save_path.with_suffix(".json").exists():
                        return self._json(409, {"error": f"Voice asset {save_name!r} already exists; choose another name."})

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
                        self._stream_line({"type": "start", **meta,
                                           "service_first_byte_s": state["first_byte_s"]})
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

                try:
                    result = engines.stream_qwen(req, begin_stream, send_audio)
                    saved_asset = None
                    if save_path is not None:
                        import numpy as np
                        raw_pcm = b"".join(state["chunks"])
                        if len(raw_pcm) != state["sample_count"] * 2:
                            raise RuntimeError("Stream capture byte count did not match delivered samples.")
                        audio = np.frombuffer(raw_pcm, dtype="<i2")
                        wav_buf = io.BytesIO()
                        sf.write(wav_buf, audio, int(result["sample_rate"]), format="WAV", subtype="PCM_16")
                        save_designed_reference(save_path, wav_buf.getvalue(), req, result,
                                                engines.entries.get(req.get("engine"), {}))
                        saved_asset = str(save_path.resolve())
                    complete = {
                        "type": "complete", "engine": result.get("engine"), "mode": result.get("mode"),
                        "streaming_kind": result.get("streaming_kind", "native_generator"),
                        "chunk_count": result.get("chunk_count"), "backend_chunk_count": result.get("backend_chunk_count"),
                        "sample_count": result.get("sample_count"), "sample_rate": result.get("sample_rate"),
                        "audio_seconds": (result.get("sample_count", 0) / result["sample_rate"]
                                          if result.get("sample_rate") else None),
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
                    }
                    self._stream_line(complete)
                except (ValueError, MemoryError) as exc:
                    if state["headers_sent"]:
                        try:
                            self._stream_line({"type": "error", "error": str(exc)})
                        except OSError:
                            pass
                    else:
                        return self._json(400, {"error": str(exc)})
                except WorkerError as exc:
                    if state["headers_sent"]:
                        try:
                            self._stream_line({"type": "error", "error": str(exc)})
                        except OSError:
                            pass
                    else:
                        return self._json(exc.status_code, {"error": str(exc)})
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    # A disconnected consumer has already cleared its own pending browser audio.
                    pass
                except Exception as exc:
                    if state["headers_sent"]:
                        try:
                            self._stream_line({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                        except OSError:
                            pass
                    else:
                        return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                self.close_connection = True
                return
            if self.path != "/api/speak":
                return self._json(404, {"error": "not found"})
            save_name = req.get("save_voice_as")
            save_path = None
            if save_name not in (None, ""):
                save_engine = req.get("engine", "kokoro")
                can_save_design = (save_engine == "breeze-tts-2" and req.get("mode", "design") == "design") or (
                    save_engine in {"qwen3-tts-1.7b-voice-design", "voxcpm2"} and req.get("mode", "design") == "design")
                if not can_save_design:
                    return self._json(400, {"error": "Reusable voice assets can be saved from Breeze, Qwen VoiceDesign, or VoxCPM2 design takes."})
                if not isinstance(save_name, str) or not VOICE_ASSET_NAME_RE.fullmatch(save_name):
                    return self._json(400, {"error": "save_voice_as must be a safe name with up to two folders; use letters, digits, underscores, or hyphens."})
                asset_root = LAB / "data" / "voices"
                asset_root.mkdir(parents=True, exist_ok=True)
                resolved_root = asset_root.resolve()
                if LAB.resolve() not in resolved_root.parents:
                    return self._json(400, {"error": "Voice asset storage must remain inside voice-lab/data/voices/."})
                save_path = resolved_root.joinpath(*save_name.split("/")).with_suffix(".wav")
                if resolved_root not in save_path.resolve().parents:
                    return self._json(400, {"error": "Voice asset path must remain inside voice-lab/data/voices/."})
                if save_path.exists() or save_path.with_suffix(".json").exists():
                    return self._json(409, {"error": f"Voice asset {save_name!r} already exists; choose another name."})
            self._note(engine=req.get("engine", "kokoro"))
            try:
                wav, sr, info = engines.speak(req)
            except (ValueError, MemoryError) as exc:
                return self._json(400, {"error": str(exc)})
            except WorkerError as exc:
                return self._json(exc.status_code, {"error": str(exc)})
            except Exception as exc:
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            buf = io.BytesIO()
            sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
            audio_body = buf.getvalue()
            headers = {
                "X-Gen-Seconds": f"{info['total_s']:.2f}",
                "X-Audio-Seconds": f"{len(wav) / sr:.2f}",
                "X-First-Chunk": f"{info['first_chunk_s']:.2f}" if info.get("first_chunk_s") else "",
            }
            if save_path is not None:
                try:
                    save_designed_reference(save_path, audio_body, req, info,
                                            engines.entries.get(req.get("engine"), {}))
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

    return Handler


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


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
    parser.add_argument("--device", default="auto", choices=("auto", "cpu"))
    parser.add_argument("--idle-minutes", type=float, default=10, help="unload models after this long idle (0 = never)")
    parser.add_argument("--state-root", default=str(LAB), help="local Cast/Scene data root (defaults to this project)")
    parser.add_argument("--preload", default="", help="comma list of engines to load at start (kokoro, turbo, orig)")
    parser.add_argument("--parent-pid", type=int, default=0, help="exit when this process ends (used by a parent launcher)")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="do not sweep stale worker folders, desktop sessions and old service logs from out/ at start")
    parser.add_argument("--desktop-session", type=Path, help=argparse.SUPPRESS)
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
                         check_store=EngineCheckStore(state_root / "data" / "engines" / "checks.json"))
    scenes = SceneStore(cast, engines.entries, root=state_root)
    evaluations = EvaluationStore(state_root / "data/evaluations/evaluations.sqlite3",
                                  lab_root=state_root)
    server = LocalThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler)
    batch_runner = SceneBatchRunner(scenes, f"http://127.0.0.1:{server.server_address[1]}")
    request_log = RequestLog(OUT / "logs")
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
        engines.close()
        server.RequestHandlerClass.close_drama()
        batch_runner.close(timeout=10)
        server.server_close()
        request_log.event("stopped")
        request_log.close()


if __name__ == "__main__":
    main()
