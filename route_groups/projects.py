"""Project search and checked local voice and project archives."""

from urllib.parse import parse_qs

import portable_projects as portable
import voice_names
from projects_index import projects, recent_projects


def register(router, lab):
    @router.get("/api/projects")
    def list_projects(self, parsed, req):
        q = parse_qs(parsed.query)
        self._json(200, {"projects": projects(lab.scenes, q.get("kind", [""])[0], q.get("q", [""])[0])})

    @router.get("/api/projects/recent")
    def recent(self, parsed, req):
        self._json(200, {"projects": recent_projects(lab.scenes)})

    @router.post("/api/portable/export")
    def export(self, parsed, req):
        fn = portable.export_persona if req.get("kind") == "persona" else portable.export_project
        self._json(201, {"archive": fn(lab.scenes, req)})

    @router.post("/api/portable/preview")
    def preview(self, parsed, req):
        archive = portable.preview(lab.scenes, self._read_body(portable.MAX_BYTES))
        for voice in archive["voices"]:
            voice["model_installed"] = voice_names.model_installed(lab.engines, voice["engine_id"])
        self._json(200, {"archive": archive})

    @router.post("/api/portable/voice-preview")
    def preview_voice(self, parsed, req):
        asset = parse_qs(parsed.query).get("asset", [""])[0]
        raw = self._read_body(portable.MAX_BYTES)
        self._send(200, portable.sample_preview(raw, asset), "audio/wav")

    @router.post("/api/portable/import")
    def load_archive(self, parsed, req):
        raw = self._read_body(portable.MAX_BYTES)
        self._json(201, {"imported": portable.import_archive(lab.scenes, raw, self._reference_metadata())})

    @router.get("/api/portable/{delivery_id}.zip")
    def fetch_archive(self, parsed, req, delivery_id):
        key = portable._id(delivery_id)
        path = lab.scenes.root / "out" / "portable" / (key + ".zip")
        if not path.is_file():
            raise KeyError("Portable archive not found.")
        self._send_path(path, "application/zip")
