"""Scenes and export: scenes (scene_store.py) made, imported from a script or a template, and exported; their lines, takes
and previews; the batch jobs that render them (SceneBatchRunner); and export packages (export_panel.py), with the FFmpeg
they may need."""

import json
from pathlib import Path
from urllib.parse import parse_qs

from book_store import Books
import connector_profiles
import dub_translation
import dubbing
import export_panel
import sample_project
import voice_names
from route_groups.cast import add_voice
from scene_store import csv_template, json_template
from script_import import FORMATS, parse_script


def register(router, lab):
    """The Scenes and Exports rows of api_spec.ROUTES."""
    serve, engines, installer, scenes, batches = lab.serve, lab.engines, lab.installer, lab.scenes, lab.batches

    @router.get("/api/scenes")
    def scene_list(self, parsed, req):
        saved = scenes.list_scenes(group_subtitles=True)
        books = Books(scenes).chapter_books() if any(scene.get("book_id") for scene in saved) else {}
        self._json(200, {"schema_version": 1, "scenes": saved, **({"chapter_books": books} if books else {})})

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
        if fmt in FORMATS:
            imported, report = scenes.import_script(content, fmt, req.get("speakers"), req.get("narrator"), req.get("name"))
            self._json(201, {"scene": imported[0], "report": report, **({"scenes": imported} if len(imported) > 1 else {})})
            return
        if fmt == "json":
            try:
                manifest = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid scene JSON: {exc}") from exc
        elif fmt == "csv":
            manifest = scenes.import_csv(content.encode("utf-8"))
        else:
            raise ValueError("Choose a JSON, CSV, plain text, Fountain, SRT or WebVTT file to import.")
        self._json(201, {"scene": scenes.create_scene(manifest)})

    def kokoro_installed():
        return voice_names.model_installed(engines, sample_project.engine()[0])

    @router.get("/api/sample")
    def sample_status(self, parsed, req):
        self._json(200, sample_project.status(scenes, lab.cast, kokoro_installed()))

    @router.post("/api/sample")
    def sample_open(self, parsed, req):
        made, sample = sample_project.open_sample(
            scenes,
            lab.cast,
            kokoro_installed(),
            lambda character_id, request: add_voice(engines, lab.cast, character_id, request),
        )
        self._json(201 if made else 200, {"made": made, "sample": sample})

    @router.get("/api/sample/remove/preview")
    def sample_removal_preview(self, parsed, req):
        self._json(200, sample_project.removal_preview(scenes, lab.cast))

    @router.post("/api/sample/remove")
    def sample_remove(self, parsed, req):
        if (req or {}).get("confirm") is not True:
            raise ValueError("Removing the sample deletes its scene, its characters and their takes. Confirm it to continue.")
        self._json(200, sample_project.remove(scenes, lab.cast))

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
                self._json(409, {"error": "Batch manifest checksum mismatch.", "code": "conflict", "details": info})
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

    @router.get("/api/dub-queue")
    def dub_queue(self, parsed, req):
        self._json(200, lab.dub_jobs.snapshot())

    @router.post("/api/dub-queue")
    def queue_dubs(self, parsed, req):
        self._json(202, {"jobs": lab.dub_jobs.enqueue(req.get("items"))})

    @router.post("/api/dub-queue/jobs/{job_id}/retry", "/api/dub-queue/jobs/{job_id}/cancel")
    def dub_job_action(self, parsed, req, job_id):
        self._json(200, {"job": lab.dub_jobs.action(job_id, parsed.path.rsplit("/", 1)[1])})

    @router.post("/api/dub-queue/watches")
    def add_dub_watch(self, parsed, req):
        self._json(201, {"watch": lab.dub_jobs.add_watch(req.get("path"))})

    @router.post("/api/dub-queue/watches/{job_id}/disable")
    def disable_dub_watch(self, parsed, req, job_id):
        self._json(200, {"watch": lab.dub_jobs.remove_watch(job_id)})

    @router.get("/api/scenes/{scene_id}/dub")
    def dub_status(self, parsed, req, scene_id):
        self._json(200, dubbing.status(scenes, scene_id))

    @router.post("/api/scenes/{scene_id}/dub/source", "/api/scenes/{scene_id}/dub/background")
    def dub_attach(self, parsed, req, scene_id):
        length = self._declared_length()
        if length > dubbing.MAX_MEDIA_BYTES:
            raise OverflowError("Dubbing media uploads are limited to 512 MiB.")
        name = (parse_qs(parsed.query).get("name") or [""])[0]
        if "\\" in name or "/" in name or any(ord(ch) < 32 for ch in name):
            raise ValueError("Supply a media file name without a directory.")
        # The browser sends a chosen local file as the raw body. Stream it to staging rather than buffering it.
        self.connection.settimeout(60)
        self._json(
            201,
            {
                "asset": dubbing.attach(
                    scenes, scene_id, self.rfile, length, name, background=parsed.path.endswith("/background")
                )
            },
        )

    @router.get("/api/scenes/{scene_id}/dub/captions/{dub_file}")
    def dub_captions(self, parsed, req, scene_id, dub_file):
        _, scene = dubbing._directory(scenes, scene_id)
        text = dubbing.captions(scene).get(dub_file)
        if text is None:
            raise KeyError("Caption file not found.")
        self._send(
            200,
            text.encode("utf-8"),
            dubbing.TYPES[dub_file.rsplit(".", 1)[1]],
            {"Content-Disposition": f'attachment; filename="{dub_file}"'},
        )

    @router.post("/api/scenes/{scene_id}/dub/translate")
    def dub_translate(self, parsed, req, scene_id):
        folder, scene = dubbing._directory(scenes, scene_id)
        if req.get("expected_revision") != scene["revision"]:
            raise ValueError("Reload the saved scene before drafting a translation.")
        ident = req.get("provider")
        profile = connector_profiles.get(ident if isinstance(ident, str) else "", "translate")
        if req.get("approve_provider_revision") != profile["revision"]:
            raise ValueError("Review the destination and approve this translation request, then try again.")
        line = next((line for line in scene["lines"] if line["line_id"] == req.get("line_id")), None)
        if line is None:
            raise ValueError("Choose a saved line to translate.")
        draft = dub_translation.draft(folder, scene, line, req, profile)
        self._json(200, {"draft": draft, "revision": scene["revision"]})

    @router.post("/api/scenes/{scene_id}/dub/exports")
    def dub_export(self, parsed, req, scene_id):
        self._json(201, {"delivery": dubbing.export(scenes, scene_id, req)})

    @router.post("/api/scenes/{scene_id}/dub/exports/{delivery_id}/verify")
    def dub_verify(self, parsed, req, scene_id, delivery_id):
        self._json(200, dubbing.verify(scenes, scene_id, delivery_id))

    @router.get(
        "/api/scenes/{scene_id}/dub/source/{dub_file}",
        "/api/scenes/{scene_id}/dub/exports/{delivery_id}/{dub_file}",
    )
    def dub_file_bytes(self, parsed, req, scene_id, dub_file, delivery_id=None):
        target = dubbing.path_for(scenes, scene_id, dub_file, delivery_id)
        self._send_path(target, dubbing.TYPES[target.suffix.lstrip(".")])

    @router.get("/api/exports")
    def export_list(self, parsed, req):
        # ?recheck=1 asks FFmpeg again, for a page whose user has just installed it.
        recheck = parse_qs(parsed.query).get("recheck") == ["1"]
        self._json(
            200, {"exports": export_panel.list_exports(scenes.root), "ffmpeg": export_panel.ffmpeg_report(refresh=recheck)}
        )

    @router.get("/api/ffmpeg/install")
    def ffmpeg_install_status(self, parsed, req):
        self._json(200, {"install": installer.snapshot(tool_key="ffmpeg")})

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
