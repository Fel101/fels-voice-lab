"""System and backups: the System tab's report on this computer and on the Lab's storage (system_report.py), library
backups, their checks and restores (library_backup.py), cleaning up out/ (housekeeping.py), checking for updates
(updates.py), and choosing the library the desktop app opens."""

from urllib.parse import parse_qs

from app_version import APP_NAME, VERSION
from common import ram_report
import export_panel
import housekeeping
import library_backup
import system_report
import updates


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
