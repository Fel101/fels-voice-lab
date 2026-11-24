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


FINISHING = "finishing"


def note_finishing(directory, what):
    """Tell the window that the service, asked to stop, is letting a model install finish its package command first
    (engine_install), so the window waits for the service instead of ending its processes, pip among them."""
    try:
        (Path(directory) / FINISHING).write_text(str(what), "utf-8")
    except OSError:
        pass


def finishing(directory):
    """The install the service said it is finishing before it stops, or None."""
    try:
        return (Path(directory) / FINISHING).read_text("utf-8")
    except OSError:
        return None
