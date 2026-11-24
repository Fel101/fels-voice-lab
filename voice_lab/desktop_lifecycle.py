"""Local file handshake for an owned desktop backend; no HTTP shutdown endpoint."""
import json
import os
from pathlib import Path
import threading
import time


def publish_session(directory, server):
    directory = Path(directory)
    request = json.loads((directory / "request.json").read_text("utf-8"))
    nonce = request["nonce"]
    if not isinstance(nonce, str) or len(nonce) != 64:
        raise ValueError("Invalid desktop session nonce")
    ready = {"nonce": nonce, "pid": os.getpid(),
             "url": f"http://127.0.0.1:{server.server_address[1]}"}
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps(ready), "utf-8")
    temporary.replace(directory / "ready.json")

    def watch():
        while True:
            try:
                if (directory / "stop").read_text("utf-8") == nonce:
                    server.shutdown()
                    return
            except OSError:
                pass
            time.sleep(.2)

    threading.Thread(target=watch, name="desktop-stop", daemon=True).start()
