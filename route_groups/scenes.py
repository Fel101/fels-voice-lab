"""Scenes and export: scenes (scene_store.py) made, imported from a script or a template, and exported; their lines, takes
and previews; the batch jobs that render them (SceneBatchRunner); and export packages (export_panel.py), with the FFmpeg
they may need."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import export_panel
from scene_store import csv_template, json_template
from script_import import parse_script


def register(router, lab):
    """The Scenes and Exports rows of api_spec.ROUTES."""
    serve, engines, installer, scenes, batches = lab.serve, lab.engines, lab.installer, lab.scenes, lab.batches

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
        self._send(
            200,
            csv_template(),
            "text/csv; charset=utf-8",
            {"Content-Disposition": "attachment; filename=voice-lab-scene-template.csv"},
        )

    def _import_content(req):
        content = req.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > serve.MAX_SCENE_REQUEST_BYTES:
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
            self._send(
                200,
                scenes.export_json(scene_id),
                "application/json; charset=utf-8",
                {"Content-Disposition": f"attachment; filename=scene-{scene_id}.json"},
            )
        except (ValueError, KeyError, RuntimeError) as exc:
            self._json(404, {"error": str(exc)})

    @router.get("/api/scenes/{scene_id}/export.csv")
    def scene_export_csv(self, parsed, req, scene_id):
        try:
            self._send(
                200,
                scenes.export_csv(scene_id),
                "text/csv; charset=utf-8",
                {"Content-Disposition": f"attachment; filename=scene-{scene_id}.csv"},
            )
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

    @router.post(
        "/api/scenes/jobs/{job_id}/pause",
        "/api/scenes/jobs/{job_id}/resume",
        "/api/scenes/jobs/{job_id}/cancel",
        "/api/scenes/jobs/{job_id}/retry",
    )
    def scene_job_action(self, parsed, req, job_id):
        action = parsed.path.rsplit("/", 1)[1]
        job = {"pause": scenes.request_pause, "resume": scenes.resume, "cancel": scenes.cancel, "retry": scenes.retry_failed}[
            action
        ](job_id)
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

    @router.get("/api/exports")
    def export_list(self, parsed, req):
        # ?recheck=1 asks FFmpeg again, for a page whose user has just installed it.
        recheck = parse_qs(parsed.query).get("recheck") == ["1"]
        self._json(
            200, {"exports": export_panel.list_exports(scenes.root), "ffmpeg": export_panel.ffmpeg_report(refresh=recheck)}
        )

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
