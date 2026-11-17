"""Speech input and short-lived local session endpoints."""

import base64
import http.client
import io
import json
import threading
import uuid
import wave
from urllib.parse import parse_qs

from local_speech_client import LocalClient
import connector_profiles
import engine_adapters
import speech_input
from live_speech import MAX_CHUNK

STREAM_TYPES = {"start", "audio", "complete", "error"}


class NativeReplyError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


def _query(parsed, key, default=None):
    return parse_qs(parsed.query).get(key, [default])[0]


def _integer(value, label, default=None):
    if value is None and default is not None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Use an integer {label}.") from exc


def _stream_headers(handler, mode):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.send_header("X-Voice-Lab-Stream", mode)
    handler.end_headers()
    handler.close_connection = True


def _native_stream(handler, lab, request, row, key):
    wire = http.client.HTTPConnection("127.0.0.1", lab.port, timeout=520)
    start = threading.Event()

    def interrupt():
        while not start.wait(0.2):
            if row.playback.is_set():
                try:
                    LocalClient(lab.port).request(f"/api/queue/{key}/cancel", {})
                except ValueError:
                    pass
                return

    threading.Thread(target=interrupt, daemon=True).start()
    try:
        wire.request(
            "POST", "/api/stream", json.dumps(request).encode(), {"Content-Type": "application/json", "X-Request-Id": key}
        )
        answer = wire.getresponse()
        if answer.status != 200:
            error = json.loads(answer.read(65536)).get("error", "Native speech stream could not start.")
            raise NativeReplyError(answer.status, error)
        _stream_headers(handler, "native-generator")
        for line in answer:
            if row.playback.is_set():
                break
            event = json.loads(line)
            if event.get("type") in STREAM_TYPES:
                handler._stream_line(event)
    finally:
        start.set()
        wire.close()


def _play(handler, lab, row, request):
    words = request.get("text")
    if not isinstance(words, str) or not 1 <= len(words.strip()) <= 500:
        raise ValueError("Use 1–500 characters for a live reply.")
    native = request.get("engine")
    if native is not None:
        adapter = engine_adapters.get(native) if isinstance(native, str) else None
        if adapter is None or not adapter.streams:
            raise ValueError("Choose a model with native streaming, or supply a buffered voice name.")
        if set(request) - {"engine", "text", "voice_description", "language", "speaker", "mode"}:
            raise ValueError("Unexpected live speech option.")
        _native_stream(handler, lab, {**request, "wait_seconds": 45}, row, "live-" + uuid.uuid4().hex)
        return
    voice = request.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        raise ValueError("Choose a ready voice from GET /api/voices.")
    raw = LocalClient(lab.port).speak(words, voice, 1, row.playback)
    with wave.open(io.BytesIO(raw)) as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError("The selected voice did not return mono 16-bit PCM.")
        rate, frames = wav.getframerate(), 0
        _stream_headers(handler, "buffered")
        handler._stream_line({"type": "start", "buffered": True, "sample_rate": rate, "sample_format": "pcm_s16le"})
        for index in range(1000):
            if row.playback.is_set():
                break
            packet = wav.readframes(4096)
            if not packet:
                break
            count = len(packet) // 2
            handler._stream_line(
                {
                    "type": "audio",
                    "sequence": index,
                    "sample_start": frames,
                    "sample_count": count,
                    "sample_rate": rate,
                    "sample_format": "pcm_s16le",
                    "data": base64.b64encode(packet).decode("ascii"),
                }
            )
            frames += count
        handler._stream_line({"type": "done", "sample_count": frames, "cancelled": row.playback.is_set()})


def register(router, lab):
    @router.post("/v1/audio/transcriptions", "/v1/audio/translations")
    def recognize_file(self, parsed, req):
        body = self._read_body(speech_input.LIMIT + 32768)
        suffix, data, fields = speech_input.multipart(self.headers.get("Content-Type"), body)
        options = speech_input.settings(fields, translate=parsed.path.endswith("/translations"))
        result = speech_input.run(lab.scenes.root, data, suffix, options)
        result = speech_input.response(result, options["format"])
        if isinstance(result, dict):
            return self._json(200, result)
        text, media = result
        self._send(200, text.encode("utf-8"), media)

    @router.get("/api/live")
    def live_info(self, parsed, req):
        self._json(
            200,
            {
                "schema_version": 1,
                "recognition_ready": speech_input.speech_available(),
                "sample_rate": 16000,
                "input_format": "pcm_s16le",
                "max_seconds": 30,
                "native_speech_engines": sorted(engine_adapters.engines_where("streams").intersection(lab.engines.api_entries)),
                "buffered_speech": True,
                "resume": "event cursor and sample offset during the service lifetime",
            },
        )

    @router.post("/api/live")
    def create_session(self, parsed, req):
        self._json(201, lab.live_sessions().create(req))

    @router.get("/api/live/{session_id}")
    def session_status(self, parsed, req, session_id):
        self._json(200, lab.live_sessions().replay(session_id, _integer(_query(parsed, "after"), "event cursor", 0)))

    @router.post("/api/live/{session_id}/audio")
    def session_audio(self, parsed, req, session_id):
        raw = self._read_body(MAX_CHUNK)
        offset = _integer(_query(parsed, "offset"), "sample offset")
        self._json(200, lab.live_sessions().push(session_id, offset, raw))

    @router.post("/api/live/{session_id}/partial", "/api/live/{session_id}/final")
    def session_recognition(self, parsed, req, session_id):
        self._json(200, lab.live_sessions().recognize(session_id, parsed.path.endswith("/final")))

    @router.post("/api/live/{session_id}/cancel")
    def cancel_session(self, parsed, req, session_id):
        self._json(200, lab.live_sessions().cancel(session_id))

    @router.post("/api/live/{session_id}/speak")
    def stream_reply(self, parsed, req, session_id):
        sessions = lab.live_sessions()
        with sessions.lock:
            row = sessions.get(session_id)
            if row.running or row.state == "cancelled":
                raise ValueError("Wait for this session's current operation or start a new session.")
            row.running = True
        try:
            _play(self, lab, row, req)
        finally:
            with sessions.lock:
                row.running = False

    @router.get("/api/automation/providers")
    def providers(self, parsed, req):
        self._json(
            200,
            {
                "schema_version": 1,
                "providers": [connector_profiles.public(row) for row in connector_profiles.configured_profiles().values()],
            },
        )

    @router.post("/api/automation/providers/check")
    def check_provider(self, parsed, req):
        name = req.get("provider")
        if not isinstance(name, str):
            raise ValueError("Choose a configured provider.")
        profile = connector_profiles.get(name)
        if req.get("approve_provider_revision") != profile["revision"]:
            raise ValueError("Review and approve the provider before checking connectivity.")
        report = json.loads(connector_profiles.exchange(profile, "/models", byte_limit=1_000_000))
        models = report.get("data") if isinstance(report, dict) else None
        if not isinstance(models, list):
            raise ValueError("The provider did not return an OpenAI-compatible model list.")
        self._json(
            200,
            {
                "reachable": True,
                "model_visible": any(isinstance(row, dict) and row.get("id") == profile["model"] for row in models),
                "provider": connector_profiles.public(profile),
            },
        )

    @router.post("/api/automation/providers")
    def save_provider(self, parsed, req):
        self._json(201, {"provider": connector_profiles.save(req.get("profile"), req.get("key", ""))})

    @router.get("/api/automation/recipes")
    def recipes(self, parsed, req):
        self._json(200, {"recipes": lab.automation().store.recipes()})

    @router.post("/api/automation/recipes")
    def save_recipe(self, parsed, req):
        self._json(201, {"recipe": lab.automation().store.save(req)})

    @router.post("/api/automation/preview")
    def preview_run(self, parsed, req):
        self._json(200, {"plan": lab.automation().store.plan(req)})

    @router.post("/api/automation/assets")
    def upload(self, parsed, req):
        data = self._read_body(speech_input.LIMIT)
        suffix = speech_input.audio_suffix(_query(parsed, "filename"))
        ident = lab.automation().store.put(data, suffix)
        self._json(201, {"asset_id": ident})

    @router.get("/api/automation/assets/{asset_sha}")
    def asset(self, parsed, req, asset_sha):
        path = lab.automation().store.asset(asset_sha)
        self._send_path(path, "audio/wav" if path.suffix == ".wav" else "application/octet-stream")

    @router.get("/api/automation/runs")
    def runs(self, parsed, req):
        self._json(200, {"runs": lab.automation().store.runs()})

    @router.post("/api/automation/runs")
    def run(self, parsed, req):
        engine = lab.automation()
        submitted = engine.store.submit(req)
        engine.start()
        self._json(202, {"run": submitted})

    @router.get("/api/automation/runs/{job_id}")
    def read_run(self, parsed, req, job_id):
        self._json(200, {"run": lab.automation().store.get(job_id)})

    @router.post("/api/automation/runs/{job_id}/cancel")
    def cancel_run(self, parsed, req, job_id):
        self._json(200, {"run": lab.automation().cancel(job_id)})
