"""Engines and speech: the models (their status, Test, Install and device), loading and unloading them, the line for the
model worker, and speaking: a clip (/api/speak), a live clip (/api/stream), a take for a Cast voice (/api/performance)
and Kokoro's pronunciation preview. Also the service's health, info, catalog and OpenAPI document."""

import json
from pathlib import Path
import re
import time

import api_spec
from app_version import VERSION
from cast_store import DIRECTION_ENGINES, generation_identity_snapshot
from common import free_ram_gb
import engine_adapters
import job_queue
from performance_settings import DEFAULTS, effective_settings
from reference_catalog import save_designed_reference
import synthetic_label
from worker_pool import WorkerError

VOICE_ASSET_NAME_RE = re.compile(r"(?:(?:[A-Za-z0-9][A-Za-z0-9_-]{0,31})/){0,2}[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")


def register(router, lab):
    """The Service and Speaking rows of api_spec.ROUTES."""
    serve, engines, installer, cast = lab.serve, lab.engines, lab.installer, lab.cast

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
        self._json(
            200,
            {
                "device": engines.device,
                "loaded": engines.loaded,
                "voices": serve.discover_kokoro_voices(),
                **engine_adapters.info(),
                "free_ram_gb": round(free_ram_gb(), 1),
            },
        )

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
        schemas = [serve.performance_schema(key, entry) for key, entry in engines.api_entries.items() if key in DEFAULTS]
        self._json(200, {"engines": schemas, "unscreened_languages": serve.unscreened_by_engine(engines.api_entries)})

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
        self._json(
            200,
            engines.prepare_reference(
                req.get("engine"),
                req.get("path"),
                exaggeration=req.get("exaggeration", 0.5),
                profile=req.get("profile"),
                reference_text=req.get("reference_text", ""),
                xvec_only=req.get("xvec_only", False),
            ),
        )

    def voice_asset_path(save_name):
        """Where a design take named save_voice_as is kept, under data/voices; ValueError for a name the Lab will not use and
        FileExistsError for one that is taken."""
        if not isinstance(save_name, str) or not VOICE_ASSET_NAME_RE.fullmatch(save_name):
            raise ValueError(
                "save_voice_as must be a safe name with up to two folders; use letters, digits, underscores, or hyphens."
            )
        asset_root = serve.LAB / "data" / "voices"
        asset_root.mkdir(parents=True, exist_ok=True)
        resolved_root = asset_root.resolve()
        if serve.LAB.resolve() not in resolved_root.parents:
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
                return self._json(
                    400, {"error": "Reusable voice assets can be saved from Breeze, Qwen VoiceDesign, or VoxCPM2 design takes."}
                )
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

        state = {
            "headers_sent": False,
            "first_byte_s": None,
            "first_audio_s": None,
            "chunks": [],
            "sample_count": 0,
            "next_sequence": 0,
            "sample_rate": None,
        }

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
            if packet["sequence"] != state["next_sequence"] or packet["sample_start"] != state["sample_count"]:
                raise RuntimeError("Stream sample offsets are discontinuous.")
            state["sample_rate"] = packet["sample_rate"]
            state["sample_count"] += packet["sample_count"]
            state["next_sequence"] += 1
            if save_path is not None:
                state["chunks"].append(packet["pcm"])
            event = {
                "type": "audio",
                "sequence": packet["sequence"],
                "sample_start": packet["sample_start"],
                "sample_count": packet["sample_count"],
                "sample_rate": packet["sample_rate"],
                "sample_format": "pcm_s16le",
                "data": packet["data"],
                "service_first_audio_s": None,
            }
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
                save_designed_reference(
                    save_path,
                    synthetic_label.wav_bytes(audio, int(result["sample_rate"]), engine_id=req.get("engine")),
                    req,
                    result,
                    engines.entries.get(req.get("engine"), {}),
                )
                saved_asset = str(save_path.resolve())
            self._stream_line(
                {
                    "type": "complete",
                    "engine": result.get("engine"),
                    "mode": result.get("mode"),
                    "streaming_kind": result.get("streaming_kind", "native_generator"),
                    "chunk_count": result.get("chunk_count"),
                    "backend_chunk_count": result.get("backend_chunk_count"),
                    "sample_count": result.get("sample_count"),
                    "sample_rate": result.get("sample_rate"),
                    "audio_seconds": (
                        result.get("sample_count", 0) / result["sample_rate"] if result.get("sample_rate") else None
                    ),
                    "generator_first_chunk_s": result.get("generator_first_chunk_s"),
                    "service_first_byte_s": state["first_byte_s"],
                    "service_first_audio_s": state["first_audio_s"],
                    "generation_total_s": result.get("total_s"),
                    "service_total_s": time.perf_counter() - started,
                    "sampling": result.get("sampling"),
                    "backend": result.get("backend"),
                    "checkpoint": result.get("checkpoint"),
                    "checkpoint_revision": result.get("checkpoint_revision"),
                    "reference_cache_hit": result.get("reference_cache_hit"),
                    "speaker": result.get("speaker"),
                    "saved_asset": saved_asset,
                }
            )
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
            raise ValueError(
                "The scene voice identity changed after this batch was saved; reload and save the scene before rendering."
            )
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
                if (
                    existing["parent_take_id"] is not None  # a key an edit of a take used is not a performance's
                    or existing["identity_id"] != identity["id"]
                    or existing["reference_version_id"] != expected_reference
                    or existing["line_id"] != line_id.strip()
                    or existing["text"] != original_text.strip()
                    or existing["spoken_text"] != spoken_text.strip()
                    or existing["language"] != language
                    or existing["direction"] != direction.strip()
                    or previous_pronunciations != (req.get("use_pronunciations") is True)
                    or effective_settings(engine_id, previous_settings) != applied_settings
                    or existing["model"].get("voice_identity") not in (None, voice_identity)
                ):
                    raise ValueError("Performance idempotency_key was already used with different inputs.")
                audio_body = Path(cast.get_media_path("take", existing["id"])).read_bytes()
                return self._send(
                    200,
                    audio_body,
                    "audio/wav",
                    {
                        "X-Take-Id": existing["id"],
                        "X-Cast-Character": existing["character_name"],
                        "X-Cast-Identity": existing["identity_label"],
                        "X-Cast-Line-Id": existing["line_id"],
                        "X-Idempotent-Replay": "true",
                    },
                )

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
            "engine_id": engine_id,
            "engine_name": catalog_entry.get("name", engine_id),
            "backend": engine_info.get("backend", catalog_entry.get("backend", {}).get("name")),
            "checkpoint": engine_info.get("checkpoint", catalog_entry.get("checkpoint")),
            "checkpoint_revision": engine_info.get("checkpoint_revision", catalog_entry.get("local_revision")),
            "runtime": catalog_entry.get("runtime"),
            "mode": generation_mode,
            "backend_result": engine_info,
            "effective_settings": applied_settings,
            "voice_identity": voice_identity,
        }
        if adapter is not None:
            model.update(adapter.take_record(voice_options))
        take = cast.create_take(
            identity,
            line_id=line_id.strip(),
            text=original_text.strip(),
            spoken_text=spoken_text.strip(),
            language=language,
            direction=direction.strip(),
            settings={**applied_settings, "use_pronunciations": req.get("use_pronunciations") is True},
            model=model,
            wav=wav,
            sample_rate=sample_rate,
            generation_info=engine_info,
            request_key=request_key,
            phoneme_timing=phoneme_timing,
        )
        audio_body = Path(cast.get_media_path("take", take["id"])).read_bytes()
        headers = {
            "X-Take-Id": take["id"],
            "X-Cast-Character": identity["character_name"],
            "X-Cast-Identity": identity["label"],
            "X-Cast-Line-Id": take["line_id"],
            "X-Gen-Seconds": str(engine_info.get("total_s", "")),
            "X-Audio-Seconds": f"{take['duration_seconds']:.2f}",
            "X-Voice-Backend": str(model.get("backend") or ""),
            "X-Model-Checkpoint": str(model.get("checkpoint") or ""),
            "X-Checkpoint-Revision": str(model.get("checkpoint_revision") or ""),
            "X-Generation-Mode": generation_mode,
        }
        return self._send(200, audio_body, "audio/wav", headers)

    @router.post("/api/kokoro/phonemize")
    def kokoro_phonemize(self, parsed, req):
        self._json(200, engines.phonemize(req.get("text", ""), req.get("language", "English")))
