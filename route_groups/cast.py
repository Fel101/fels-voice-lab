"""The Cast and its voice recordings (cast_store.py): characters and their voices, reference recordings and their versions,
transcript suggestions, pronunciations, retiring and purging; the lists of references and saved voices the page reads
(reference_catalog.py); and Voice Slate's clips, picked by hand or waiting in the inbox (slate_bundle.py, slate_inbox.py)."""

import base64
import json
from threading import Lock
from urllib.parse import parse_qs, unquote

from cast_store import MAX_UPLOAD_BYTES, PRESET_VOICE_ENGINES, REFERENCE_ENGINES
import engine_adapters
from performance_settings import performance_controls
from reference_catalog import list_references, list_voice_library, resolve_reference_audio
import transcript_suggest
from recorder_import import import_clip
from slate_bundle import MAX_BUNDLE_BYTES, read_bundle
from slate_inbox import NotCleared, NotWaiting


def add_voice(engines, cast, character_id, req):
    """Add a voice to a character, as POST /api/characters/{character_id}/identities asks: an engine that clones from a
    recording, or one with fixed or preset voices, whose adapter turns the request into the voice's options. The sample
    scene (sample_project.py) makes its voices here too."""
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
    return cast.create_identity(character_id, engine_id, req.get("label"), options, req.get("notes", ""))


def register(router, lab):
    """The Cast rows of api_spec.ROUTES, and the lists of references and saved voices the page reads."""
    serve, engines, cast = lab.serve, lab.engines, lab.cast

    slate_import_lock = Lock()

    def cloning_engines():
        return sorted(REFERENCE_ENGINES & engines.api_entries.keys())

    def import_slate_clip(raw, *, character, engine, create_character, accept_draft, reviewed, changed):
        """Check a .voiceslate file's bytes and import its clip as a candidate: the file box and the inbox both come here.
        `reviewed` is the sidecar hash the person previewed, if any; `changed` is the sentence when the clip differs."""
        if engine not in REFERENCE_ENGINES or engine not in engines.api_entries:
            raise ValueError("Choose a model that can use a voice recording.")
        clip = read_bundle(raw, accept_asr_draft=accept_draft)
        if reviewed is not None and reviewed != clip.sidecar_sha256:
            raise ValueError(changed)
        with slate_import_lock:
            result = import_clip(
                clip,
                character=character,
                engine_id=engine,
                create_character=create_character,
                store=cast,
                identity_check=False,
            )
        return {key: result[key] for key in ("status", "character_id", "identity_id", "reference_id", "warnings")}

    @router.post("/api/cast/slate-clip/preview")
    def preview_slate_clip(self, parsed, req):
        clip = read_bundle(self._read_body(MAX_BUNDLE_BYTES), accept_asr_draft=True)
        self._json(200, {"clip": clip.summary(), "engines": cloning_engines()})

    @router.post("/api/cast/slate-clip/import")
    def register_slate_clip(self, parsed, req):
        params = parse_qs(parsed.query)
        reviewed = params.get("sidecar_sha256")
        answer = import_slate_clip(
            self._read_body(MAX_BUNDLE_BYTES),
            character=params.get("character", [""])[0],
            engine=params.get("engine_id", [""])[0],
            create_character=params.get("create_character") == ["1"],
            accept_draft=params.get("accept_draft") == ["1"],
            reviewed=reviewed[0] if reviewed else None,
            changed="The clip changed after preview. Pick the handoff file again to review it.",
        )
        self._json(201, answer)

    # The inbox (slate_inbox.py): files Voice Slate hands over wait there until the person imports or dismisses them.
    @router.get("/api/inbox")
    def inbox_list(self, parsed, req):
        self._json(200, {"files": lab.inbox.waiting(), "engines": cloning_engines(), "folder": str(lab.inbox.folder())})

    def waiting_name(inbox_file):
        name = unquote(inbox_file)
        if not name or name.startswith(".") or "/" in name or "\\" in name:
            raise NotWaiting(f"{name or 'That file'} is not a file in the inbox.")
        return name

    @router.post("/api/inbox/{inbox_file}/import")
    def inbox_import(self, parsed, req, inbox_file):
        entry, raw = lab.inbox.read(waiting_name(inbox_file))
        for key in ("create_character", "accept_draft"):
            if not isinstance(req.get(key, False), bool):
                raise ValueError(f"{key} must be true or false.")
        reviewed = req.get("sidecar_sha256")
        if reviewed is not None and not isinstance(reviewed, str):
            raise ValueError("sidecar_sha256 must be the hash from GET /api/inbox.")
        answer = import_slate_clip(
            raw,
            character=req.get("character") if isinstance(req.get("character"), str) else "",
            engine=req.get("engine_id") if isinstance(req.get("engine_id"), str) else "",
            create_character=req.get("create_character", False),
            accept_draft=req.get("accept_draft", False),
            reviewed=reviewed,
            changed="The clip changed after preview. Preview it again before you import it.",
        )
        try:
            lab.inbox.clear(entry)
            answer["cleared"] = True
        except NotCleared as exc:
            answer["cleared"] = False
            answer["warnings"] = [*answer["warnings"], f"The clip is in Cast, but its file stays in the inbox: {exc}"]
        self._json(201, answer)

    @router.post("/api/inbox/{inbox_file}/dismiss")
    def inbox_dismiss(self, parsed, req, inbox_file):
        lab.inbox.dismiss(waiting_name(inbox_file))
        self._json(200, {"ok": True})

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
        characters = cast.list_cast()
        for character in characters:
            for voice in character["identities"]:
                adapter = engine_adapters.get(voice["engine_id"])
                languages = adapter.voice_languages(voice["engine_options"]) if adapter else None
                if languages:
                    voice["voice_languages"] = languages
                if voice["status"] == "approved" and not voice["retired"]:
                    approved = voice["approved_reference_id"]
                    if any(ref["id"] == approved and not ref["retired"] for ref in voice["references"]):
                        try:
                            voice["approved_recording_available"] = cast.get_media_path("reference", approved).is_file()
                        except (OSError, KeyError, ValueError):
                            voice["approved_recording_available"] = False
        self._json(200, {"schema_version": 1, "characters": characters})

    @router.post("/api/characters")
    def create_character(self, parsed, req):
        self._json(201, {"character": cast.create_character(req.get("name"), req.get("description", ""), req.get("tags"))})

    @router.post("/api/characters/{character_id}")
    def update_character(self, parsed, req, character_id):
        self._json(200, {"character": cast.update_character(character_id, req)})

    @router.post("/api/characters/{character_id}/identities")
    def create_identity(self, parsed, req, character_id):
        self._json(201, {"identity": add_voice(engines, cast, character_id, req)})

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
            self._json(404, {"error": lab.serve.error_text(exc)})

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
            self._json(404, {"error": lab.serve.error_text(exc)})

    @router.post("/api/references/{reference_id}/transcript-suggestion")
    def transcript_suggestion(self, parsed, req, reference_id):
        # A draft only: nothing is saved or approved until the user checks and saves the text.
        try:
            audio = cast.get_media_path("reference", reference_id)
            self._json(200, {"suggestion": transcript_suggest.suggest(audio)})
        except (KeyError, FileNotFoundError) as exc:
            self._json(404, {"error": lab.serve.error_text(exc)})
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
            self._json(404, {"error": lab.serve.error_text(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    @router.post("/api/purges")
    def purge(self, parsed, req):
        self._json(200, cast.purge(req.get("kind"), req.get("id"), reason=req.get("reason", ""), confirm=req.get("confirm")))
