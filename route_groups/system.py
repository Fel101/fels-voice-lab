"""System and backups: the System tab's report on this computer and on the Lab's storage (system_report.py), library
backups, their checks and restores (library_backup.py), cleaning up out/ (housekeeping.py), checking for updates
(updates.py), Update now (self_update.py), and choosing the library the desktop app opens."""

import sys
from urllib.parse import parse_qs

from app_version import APP_NAME, REPOSITORY, VERSION
from common import LAB, ram_report
import export_panel
import housekeeping
import lab_address
import library_backup
import system_report
import updates
from self_update import UpdateNowError


def register(router, lab):
    """The System rows of api_spec.ROUTES, with the library choice and the update checks the page asks for."""
    serve, engines, log, library_root, preferences, checks, backups_folder, backup_task, logged = (
        lab.serve,
        lab.engines,
        lab.log,
        lab.library_root,
        lab.preferences,
        lab.checks,
        lab.backups_folder,
        lab.backup_task,
        lab.logged,
    )

    @router.post("/api/system/use-library")
    def use_library(self, parsed, req):
        self._json(
            200, library_backup.use_library(serve.APP_ROOT, library_backup.restore_target(library_root, req.get("folder")))
        )

    @router.get("/api/updates")
    def update_state(self, parsed, req):
        self._json(200, checks.state(preferences.get()["check_updates"]))

    @router.post("/api/updates/check")
    def update_check(self, parsed, req):
        try:
            self._json(200, checks.check_now())
        except updates.UpdateError as exc:
            self._json(502, {"error": str(exc)})

    def update_blocked():
        """Why Update now must wait, or None: closing the Lab would stop an install, a backup or a batch half done."""
        for kind in ("engine", "tool"):
            job = lab.installer.snapshot(kind)
            if job and job["state"] == "running":
                return f"{job['name']} is installing. Update now works once it has finished."
        task = backup_task.status()
        if task.get("status") == "running":
            return f"{task['label']} is running. Update now works once it has finished."
        if lab.scenes.rendering_jobs():
            return "A scene batch is rendering. Wait for it to finish, or stop it in Scenes, then press Update now."
        return None

    @router.post("/api/update/plan")
    def update_plan(self, parsed, req):
        try:
            plan = lab.updater.plan()
        except UpdateNowError as exc:
            return self._json(502, {"error": str(exc)})
        self._json(200, {"plan": plan, "blocked": update_blocked(), "how": lab.updater.how})

    @router.post("/api/update/start")
    def update_start(self, parsed, req):
        reason = update_blocked()
        if reason:
            return self._json(409, {"error": reason})
        try:
            result = lab.updater.start(self.server.shutdown)
        except UpdateNowError as exc:
            return self._json(409, {"error": str(exc)})
        log.event(f"update now: the Lab closes for the updater ({result['how']})")
        self._json(202, result)

    @router.get("/api/update/notice")
    def update_notice(self, parsed, req):
        self._json(200, {"notice": lab.updater.notice()})

    @router.post("/api/update/notice/dismiss")
    def update_notice_dismiss(self, parsed, req):
        lab.updater.dismiss()
        self._json(200, {"ok": True})

    @router.post("/api/system/open-folder")
    def open_folder(self, parsed, req):
        try:
            self._json(200, lab.updater.open_folder())
        except (UpdateNowError, OSError) as exc:
            self._json(400, {"error": f"The folder could not be opened: {exc}"})

    @router.get("/api/system/connect")
    def connect(self, parsed, req):
        url = f"http://127.0.0.1:{lab.port}"
        self._json(
            200,
            {
                "url": url,
                "known_url": lab_address.KNOWN_URL,
                "note": lab.address_note,
                "in_address_file": lab.in_address_file,
                "address_file": str(lab_address.address_file()),
                "window_port": lab_address.window_port(serve.APP_ROOT),
                "test": f"curl.exe {url}/api/health",
                "guide": f"https://github.com/{REPOSITORY}/blob/v{VERSION}/docs/API.md",
                # The MCP server runs with the Python this service runs with, from the Lab's folder.
                "mcp": lab_address.mcp_settings(LAB, sys.executable),
                "connect_guide": f"https://github.com/{REPOSITORY}/blob/v{VERSION}/docs/CONNECT.md",
                # The base address apps that speak to OpenAI's speech API are given (POST /v1/audio/speech).
                "openai_base": f"{url}/v1",
            },
        )

    @router.post("/api/system/connect/port")
    def connect_port(self, parsed, req):
        port = lab_address.save_window_port(serve.APP_ROOT, req.get("port"))
        self._json(200, {"window_port": port})

    @router.get("/api/system")
    def system(self, parsed, req):
        recheck = parse_qs(parsed.query).get("recheck") == ["1"]
        ram = ram_report()
        report = system_report.build(
            app=APP_NAME,
            version=VERSION,
            state_root=library_root,
            out_dir=serve.OUT,
            device=engines.device,
            ffmpeg=export_panel.ffmpeg_report(refresh=recheck),
            engine_status=engines.engine_status(),
            log_path=getattr(log, "path", None),
            free_ram=ram["free_gb"],
            total_ram=ram["total_gb"],
        )
        report["diagnostics"] = system_report.diagnostics_text(report)
        self._json(200, report)

    @router.get("/api/system/storage")
    def storage(self, parsed, req):
        self._json(200, system_report.storage(library_root, serve.OUT))

    @router.get("/api/system/backups")
    def backups(self, parsed, req):
        self._json(
            200,
            {
                "folder": str(backups_folder),
                "backups": library_backup.list_backups(backups_folder),
                "plan": library_backup.plan(library_root),
                "task": backup_task.status(),
                "suggested_folder": library_backup.suggested_folder(library_root),
            },
        )

    @router.get("/api/system/task")
    def task(self, parsed, req):
        self._json(200, {"task": backup_task.status()})

    def start_task(self, kind, label, work):
        """Run a backup, check or restore in the background; one at a time, and the page polls /api/system/task."""
        try:
            state = backup_task.start(kind, label, work)
        except library_backup.BackupBusy as exc:
            return self._json(409, {"error": str(exc)})
        return self._json(202, {"task": state})

    @router.post("/api/system/backups")
    def back_up(self, parsed, req):
        include = req.get("include", list(library_backup.OPTIONAL))
        if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
            raise ValueError("include must be a list of section names.")
        library_backup.chosen_sections(include)  # an unknown name is refused now, not after the thread starts
        start_task(
            self,
            "backup",
            "Backing up the library",
            logged(
                "backup",
                lambda progress: library_backup.create(
                    library_root, backups_folder, include=include, app=APP_NAME, version=VERSION, progress=progress
                ),
                lambda result: f"{result['name']}, {result['files']} files, {result['bytes']} bytes",
            ),
        )

    @router.post("/api/system/backups/verify")
    def check_backup(self, parsed, req):
        source = library_backup.backup_path(backups_folder, req.get("name"))
        start_task(
            self,
            "check",
            f"Checking {source.name}",
            logged(
                "check",
                lambda progress: library_backup.verify(source, progress=progress),
                lambda result: f"{result['name']} {'passed' if result['ok'] else 'did not pass'}",
            ),
        )

    @router.post("/api/system/backups/restore")
    def restore_backup(self, parsed, req):
        source = library_backup.backup_path(backups_folder, req.get("name"))
        target = library_backup.restore_target(library_root, req.get("folder"))
        start_task(
            self,
            "restore",
            f"Restoring {source.name}",
            logged(
                "restore",
                lambda progress: {**library_backup.restore(source, target, progress=progress), "folder": target.name},
                lambda result: f"{result['folder']}, {result['files']} files",
            ),
        )

    @router.post("/api/system/cleanup")
    def cleanup(self, parsed, req):
        log_file = getattr(log, "path", None)
        result = housekeeping.apply(housekeeping.plan(serve.OUT, keep=[log_file] if log_file else []), serve.OUT)
        log.event(
            f"cleanup on request removed {result['removed']} items ({result['bytes']} bytes)"
            + (f"; {len(result['errors'])} could not be removed" if result["errors"] else "")
        )
        self._json(200, result)
