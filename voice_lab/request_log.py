"""One text log per service run: what each action did, how long it took and why it failed.

The file is out/logs/service-<start time>-<pid>.log. It holds a line for every request that changed something or went
wrong; page loads and status polls that succeed are left out, so the file stays readable. Request bodies are never
written, the query string is dropped, and a failing request's message is cut to 300 characters. A failure also keeps
the worker's own log path, because the error text names it.
"""

import json
import logging
import logging.handlers
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
MAX_MESSAGE = 300
MAX_PATH = 200
SLOW_SECONDS = 1.0


def new_request_id(supplied=None):
    """Reuse a caller's own request ID when it is plain text, so its logs and ours can be matched."""
    return supplied if supplied and REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex[:12]


def _text(value):
    return json.dumps(str(value)[:MAX_MESSAGE], ensure_ascii=False)


class NullRequestLog:
    """Used where nobody asked for a log, such as the tests."""
    path = None

    def start(self, *args, **facts):
        pass

    def wants(self, method, status, seconds):
        return False

    def request(self, *args, **fields):
        pass

    def event(self, message):
        pass

    def close(self):
        pass


class RequestLog:
    def __init__(self, directory, *, max_bytes=2_000_000, backups=2):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = Path(directory) / f"service-{stamp}-{os.getpid()}.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"voice_lab.service.{self.path.stem}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%Y-%m-%d %H:%M:%S"))
        self._logger.addHandler(self._handler)

    def start(self, application, version, **facts):
        details = " ".join(f"{key}={value}" for key, value in facts.items())
        self._logger.info("start %s %s pid=%s %s", application, version, os.getpid(), details)

    def wants(self, method, status, seconds):
        """Actions and failures, not the browser reading pages or polling a job that is going fine."""
        return method != "GET" or status == 0 or status >= 400 or seconds >= SLOW_SECONDS

    def request(self, request_id, method, path, status, seconds, traceback=None, **fields):
        parts = [request_id, method, path[:MAX_PATH], str(status), f"{seconds:.3f}s"]
        for key, value in fields.items():
            if value in (None, ""):
                continue
            parts.append(f"{key}={_text(value) if key == 'error' else value}")
        self._logger.info(" ".join(parts))
        if traceback:
            for line in traceback.rstrip().splitlines():
                self._logger.info("%s   | %s", request_id, line)

    def event(self, message):
        self._logger.info("event %s", message)

    def close(self):
        self._logger.removeHandler(self._handler)
        self._handler.close()
