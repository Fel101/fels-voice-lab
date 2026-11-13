"""The Cast and its voice recordings (cast_store.py): characters and their voices, reference recordings and their versions,
transcript suggestions, pronunciations, retiring and purging; and the lists of references and saved voices the page
reads (reference_catalog.py)."""

import base64
import json
from urllib.parse import parse_qs

from cast_store import MAX_UPLOAD_BYTES, PRESET_VOICE_ENGINES
import engine_adapters
from performance_settings import performance_controls
from reference_catalog import list_references, list_voice_library, resolve_reference_audio
import transcript_suggest


def register(router, lab):
    """The Cast rows of api_spec.ROUTES, and the lists of references and saved voices the page reads."""
    serve, engines, cast = lab.serve, lab.engines, lab.cast

    @router.get("/api/references")
    def reference_list(self, parsed, req):
        self._json(200, {"references": list_references(serve.LAB, cast)})

    @router.get("/api/voice-library")
    def voice_library(self, parsed, req):
        params = parse_qs(parsed.query)
        self._json(200, list_voice_library(serve.LAB, cast, include_retired=(params.get("include_retired") or [""])[0] == "1"))

    @router.get("/api/reference-audio")
    def reference_audio(self, parsed, req):
        try:
            requested = (parse_qs(parsed.query).get("path") or [None])[0]
            target, content_type = resolve_reference_audio(requested, serve.LAB, cast)
            self._send(200, target.read_bytes(), content_type)
        except (OSError, RuntimeError, ValueError):
            self._json(404, {"error": "Reference audio is unavailable or outside this Lab's reference storage."})

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
        preset_voice = engine_id in PRESET_VOICE_ENGINES and capabilities.get("voice_selection", {}).get("status") in {
            "supported",
            "worker_verified",
        }
        adapter = engine_adapters.get(engine_id)
        fixed_voices = adapter is not None and adapter.fixed_voices and fixed_voice
        if clone_status not in {"supported", "worker_verified"} and not fixed_voices and not preset_voice:
            raise ValueError("This model does not provide a reusable clone reference or a fixed voice selector.")
        if adapter is not None and (adapter.fixed_voices or adapter.preset_voices):
            options = adapter.identity_options(capabilities, req, performance_controls(engine_id, capabilities), engines)
        else:
            options = {}
        self._json(
            201, {"identity": cast.create_identity(character_id, engine_id, req.get("label"), options, req.get("notes", ""))}
        )

    @router.post("/api/identities/{identity_id}/update")
    def update_identity(self, parsed, req, identity_id):
        self._json(200, {"identity": cast.update_identity(identity_id, req, fixed_voices=serve.discover_kokoro_voices())})

    @router.post("/api/identities/{identity_id}/references/preview")
    def preview_reference(self, parsed, req, identity_id):
        try:
            body = self._read_body(MAX_UPLOAD_BYTES)
            metadata = self._reference_metadata()
            audio, metrics = cast.preview_reference(identity_id, metadata.get("filename"), body, edits=metadata.get("edits"))
        except RuntimeError as exc:
            return self._json(400, {"error": str(exc)})
        quality = json.dumps(metrics["quality"], separators=(",", ":")).encode("ascii")
        self._send(
            200,
            audio,
            "audio/wav",
            {
                "X-Reference-Duration": f"{metrics['duration_seconds']:.3f}",
                "X-Reference-Sample-Rate": str(metrics["sample_rate"]),
                "X-Reference-RMS": f"{metrics['rms_dbfs']:.1f}",
                "X-Reference-Quality": base64.urlsafe_b64encode(quality).decode("ascii"),
            },
        )

    @router.post("/api/identities/{identity_id}/references")
    def upload_reference(self, parsed, req, identity_id):
        try:
            body = self._read_body(MAX_UPLOAD_BYTES)
            metadata = self._reference_metadata()
            saved = cast.add_reference(
                identity_id,
                metadata.get("filename"),
                body,
                transcript=metadata.get("transcript", ""),
                origin=metadata.get("origin", ""),
                allowed_use=metadata.get("allowed_use", ""),
                edits=metadata.get("edits"),
            )
        except RuntimeError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(201, {"reference": saved})

    @router.post("/api/identities/{identity_id}/references/from-take")
    def reference_from_take(self, parsed, req, identity_id):
        self._json(
            201,
            {
                "reference": cast.add_reference_from_take(
                    identity_id,
                    req.get("take_id"),
                    transcript=req.get("transcript"),
                    edits=req.get("edits"),
                    origin=req.get("origin"),
                    allowed_use=req.get("allowed_use"),
                )
            },
        )

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
            content_type = {
                ".wav": "audio/wav",
                ".flac": "audio/flac",
                ".ogg": "audio/ogg",
                ".mp3": "audio/mpeg",
                ".m4a": "audio/mp4",
            }.get(target.suffix.lower(), "application/octet-stream")
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
        self._json(
            200,
            {
                "pronunciation": cast.save_pronunciation(
                    source=req.get("source"),
                    replacement=req.get("replacement"),
                    character_id=req.get("character_id"),
                    notes=req.get("notes", ""),
                )
            },
        )

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
