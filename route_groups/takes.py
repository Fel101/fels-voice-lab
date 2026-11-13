"""Takes (cast_store.py): the list, one take with its audio, waveform and voice match, its notes and choices, and edits to
its audio."""

from urllib.parse import parse_qs

import speaker_similarity


def register(router, lab):
    """The Takes rows of api_spec.ROUTES."""
    cast = lab.cast

    @router.get("/api/takes")
    def take_list(self, parsed, req):
        params = parse_qs(parsed.query)
        self._json(
            200, {"takes": cast.list_takes((params.get("identity_id") or [None])[0], (params.get("line_id") or [None])[0])}
        )

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
        self._json(
            200,
            {"take": cast.set_take_state(take_id, status=req.get("status"), rating=req.get("rating"), notes=req.get("notes"))},
        )

    @router.post("/api/takes/{take_id}/edit")
    def edit_take(self, parsed, req, take_id):
        edit_key = req.pop("idempotency_key", None)
        take, replayed = cast.create_edited_take(take_id, req, request_key=edit_key)
        self._json(200 if replayed else 201, {"take": take, "replayed": replayed})

    @router.post("/api/takes/{take_id}/edit-preview")
    def preview_take_edit(self, parsed, req, take_id):
        audio, report = cast.preview_edit(take_id, req)
        self._send(
            200,
            audio,
            "audio/wav",
            {
                "X-Audio-Seconds": f"{report['duration_seconds']:.2f}",
                "X-Edit-Start-Seconds": f"{report['start_seconds']:.3f}",
                "X-Edit-End-Seconds": f"{report['end_seconds']:.3f}",
                "X-Peak-Dbfs": "" if report["peak_dbfs"] is None else f"{report['peak_dbfs']:.2f}",
            },
        )
