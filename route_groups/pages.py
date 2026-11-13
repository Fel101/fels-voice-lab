"""The pages: the studio (serve.html) and the comparison page (compare.html) with the person's saved choices written into
them (ui_preferences.py), the scripts they load, and the files under out/ that they play."""


def register(router, lab):
    """The Pages rows of api_spec.ROUTES, and the page's saved choices (/api/preferences)."""
    serve, preferences = lab.serve, lab.preferences

    @router.get("/", "/index.html")
    def main_page(self, parsed, req):
        self._send(200, preferences.render(serve.PAGE), "text/html; charset=utf-8")

    @router.get("/compare", "/compare.html")
    def compare_page(self, parsed, req):
        self._send(200, preferences.render(serve.COMPARE_PAGE), "text/html; charset=utf-8")

    @router.get(
        "/theme.js",
        "/voice_workflows.js",
        "/voice_library.js",
        "/drama3-studio.js",
        *(f"/{name}" for name in sorted(serve.PAGE_SCRIPTS)),
    )
    def page_script(self, parsed, req):
        name = "drama3_studio.js" if parsed.path == "/drama3-studio.js" else parsed.path.lstrip("/")
        self._send(200, (serve.LAB / name).read_bytes(), "text/javascript; charset=utf-8")

    @router.get("/out/*")
    def out_file(self, parsed, req, rest):
        """Files the Lab wrote to its out folder, such as a converted voice."""
        parts = rest.split("/")
        # Plain names only. An empty or ".." part, a drive or a backslash could name something outside out/, and
        # //server/share/x.wav would have Windows contact that server before the check below could refuse it.
        if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
            return self._json(404, {"error": "not found"})
        target = serve.OUT.joinpath(*parts).resolve()
        ctype = {".wav": "audio/wav", ".json": "application/json"}.get(target.suffix)
        if ctype and serve.OUT.resolve() in target.parents and target.is_file():
            self._send(200, target.read_bytes(), ctype)
        else:
            self._json(404, {"error": "not found"})

    @router.get("/api/preferences")
    def get_preferences(self, parsed, req):
        self._json(200, preferences.get())

    @router.post("/api/preferences")
    def save_preferences(self, parsed, req):
        try:
            self._json(200, preferences.save(req))
        except OSError:
            self._json(500, {"error": "Could not save appearance preferences."})
