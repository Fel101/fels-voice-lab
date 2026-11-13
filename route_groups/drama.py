"""The Drama studio (/api/drama3/...): what its page reads, and its jobs, which drama3_lab_studio.py runs. The studio starts
the first time a route asks for it (serve.Lab.drama)."""

import re
from urllib.parse import parse_qs


def register(router, lab):
    """The Drama rows of api_spec.ROUTES."""
    drama = lab.drama

    @router.get(
        "/api/drama3/*",
        "/api/drama3/info",
        "/api/drama3/jobs/{job_id}",
        "/api/drama3/jobs/{job_id}/audio",
        "/api/drama3/jobs/{job_id}/metadata",
        "/api/drama3/jobs/{job_id}/package",
    )
    def drama_read(self, parsed, req, job_id=None, rest=None):
        from drama3_production import capabilities

        try:
            studio = drama()
            suffix = parsed.path[len("/api/drama3/") :]
            if suffix == "info":
                return self._json(200, {**capabilities(), "sources": studio.sources(), "jobs": studio.list()})
            match = re.fullmatch(r"jobs/([a-f0-9]{32})(?:/(audio|metadata|package))?", suffix)
            if not match:
                return self._json(404, {"error": "Drama endpoint not found"})
            if match.group(2):
                data, ctype = studio.artifact(*match.groups())
                disposition = (
                    "inline"
                    if match.group(2) == "audio"
                    else "attachment; filename=drama-take." + ("zip" if match.group(2) == "package" else "json")
                )
                return self._send(200, data, ctype, {"Content-Disposition": disposition})
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            self._json(200, studio.get(match.group(1), offset))
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})

    @router.post("/api/drama3/jobs")
    def start_drama_job(self, parsed, req):
        self._json(202, {"job": drama().start(req)})

    @router.post("/api/drama3/jobs/{job_id}/cancel")
    def cancel_drama_job(self, parsed, req, job_id):
        self._json(200, {"job": drama().cancel(job_id)})
