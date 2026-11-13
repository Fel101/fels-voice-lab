"""Comparisons (evaluation_store.py): the suites, saved runs, takes brought in from the Cast, blind listening sessions and
their ratings, and the transcript of what a listener heard."""

from evaluation_cast import import_cast_takes
from evaluation_suites import list_suites


def register(router, lab):
    """The Comparisons rows of api_spec.ROUTES."""
    cast, evaluations = lab.cast, lab.evaluations

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
        self._json(
            201,
            {
                "run": import_cast_takes(
                    cast, evaluations, req.get("take_ids"), title=req.get("title", "Saved voice comparison")
                )
            },
        )

    @router.get("/api/evaluations/blind-sessions")
    def blind_sessions(self, parsed, req):
        self._json(200, {"sessions": evaluations.list_blind_sessions()})

    @router.post("/api/evaluations/blind-sessions")
    def create_blind_session(self, parsed, req):
        self._json(
            201, {"session": evaluations.create_blind_session(req.get("run_id"), req.get("evaluator", "local listener"))}
        )

    @router.get("/api/evaluations/blind-sessions/{session_id}")
    def blind_session(self, parsed, req, session_id):
        try:
            self._json(200, {"session": evaluations.get_blind_session(session_id)})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})

    @router.post("/api/evaluations/blind-sessions/{session_id}/ratings")
    def rate_blind_session(self, parsed, req, session_id):
        self._json(
            200, {"session": evaluations.save_rating(session_id, req.get("trial_id"), req.get("ratings"), req.get("notes", ""))}
        )

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
