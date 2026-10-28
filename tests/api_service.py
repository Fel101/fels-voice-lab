"""A running Voice Lab service on a temporary library, with fake engines, for the tests of the HTTP API and the command-line tool."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import serve
from library_fixture import temp_cast
from worker_pool import WorkerBusy, read_catalog

RATE = 24_000


class FakeEngines:
    """Enough of EnginePool for speak, performance and the read routes. It answers a tenth of a second of tone, keeps what it was
    asked in `requests`, and can be told to hold a request (`gate`) or refuse it as busy (`busy`)."""

    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"
        self.loaded = []
        self.requests = []
        self.health_calls = 0               # how many times a client asked whether it is ready
        self.busy = False
        self.extra_info = {}                # what the engine reports besides its timings: backend, checkpoint, mode...
        self.gate = None                    # a threading.Event: speak waits for it to be set
        self.started = threading.Event()    # set when speak has been called

    def health(self):
        self.health_calls += 1
        return {"status": "ready", "busy": False, "device": "cpu", "workers": [], "last_worker_error": None, "last_unload": []}

    def engine_status(self):
        return {"device": "cpu", "busy": False, "engines": [], "speed": [], "store_error": None}

    def speak(self, request):
        self.requests.append(dict(request))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(15)
        if self.busy:
            raise WorkerBusy("The GPU worker is busy with another load or generation request.")
        if request.get("text") == "bad":
            raise ValueError("The text is not allowed.")
        axis = np.arange(RATE // 10, dtype=np.float32) / RATE
        info = {"total_s": 0.1, "first_chunk_s": None, **self.extra_info}
        return (0.1 * np.sin(2 * np.pi * 260 * axis)).astype(np.float32), RATE, info


class Service:
    """The real handler (serve.make_handler) on a temporary library, listening on a free port until the test ends."""

    def __init__(self, case, engines=None, port=0):
        self.engines = engines or FakeEngines()
        self.cast = temp_cast(case)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.port, self.cast)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        case.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        """(status, headers, bytes) for one call. An error status is returned, not raised."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def get_json(self, path):
        status, _headers, raw = self.request("GET", path)
        assert status == 200, (status, raw)
        return json.loads(raw)

    def add_voice(self, character="Nell", label="Warm", approved=False):
        """A voice for a character, a Kokoro voice, which needs no recording. Returns the identity."""
        made = self.cast.create_character(character, "") if isinstance(character, str) else character
        identity = self.cast.create_identity(made["id"], "kokoro", label, {"voice": "af_heart"})
        if approved:
            identity = self.cast.update_identity(identity["id"], {"status": "approved"})
        return identity
