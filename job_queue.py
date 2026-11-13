"""Who has the model worker now, and who waits for it, in order.

The service runs one load, clip, test or install at a time (worker_pool.EnginePool). Each takes a ticket here. A ticket that
cannot run at once is refused, as it always was (WorkerBusy, 409), unless its request said how long it will wait: then it
joins the line. People (priority "normal") go ahead of scene batch rows ("low"), and each priority keeps its own order. At
most MAX_WAITING tickets wait, and none longer than MAX_WAIT_SECONDS.

A waiting ticket leaves the line when it is cancelled, when its caller has gone away, or when its time is up, and its
request ends at once. A running ticket can only be stopped by ending the model's process: cancel() calls the stop function
its owner gave it, and the owner ends the request with WorkerCancelled (499). Standard library only, apart from the
worker's error classes.
"""

import itertools
import threading
import time
from datetime import datetime, timezone

from worker_process import WorkerCancelled, WorkerError

MAX_WAITING = 20
MAX_WAIT_SECONDS = 3600
PRIORITIES = {"normal": 0, "low": 1}


class WorkerBusy(WorkerError):
    status_code = 409


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


class Ticket:
    """One request's place: what it is (for the page: "Take", "Model test", "Install VoxCPM2"), its model when known, and
    whether it may be stopped while it runs. `alive`, when given, says whether the caller is still there."""

    def __init__(self, request_id=None, what="Model work", *, priority="normal", engine=None, can_stop=True, alive=None):
        if priority not in PRIORITIES:
            raise ValueError("wait_priority must be normal or low.")
        self.id = request_id
        self.what = what
        self.priority = priority
        self.engine = engine
        self.can_stop = can_stop
        self.alive = alive
        self.queued_at = time.time()
        self.started_at = None
        self.cancelled = False
        self.stop = None  # set by the owner while the ticket runs; it ends the work
        self.held_for = None  # an install's hold: what it is doing, for the refusals
        self.order = None

    def describe(self, now=None):
        now = time.time() if now is None else now
        since = self.started_at if self.started_at is not None else self.queued_at
        return {
            "id": self.id,
            "what": self.what,
            "engine": self.engine,
            "priority": self.priority,
            "since": _stamp(since),
            "seconds": round(now - since, 1),
            "can_stop": bool(self.can_stop and self.stop is not None),
            "detail": self.held_for,
        }


class JobQueue:
    def __init__(self):
        self._cond = threading.Condition()
        self._running = None
        self._waiting = []
        self._count = itertools.count()

    @property
    def busy(self):
        return self._running is not None

    @property
    def running(self):
        return self._running

    def take(self, ticket, wait_seconds=0, refusal="The model worker is busy."):
        """Give the worker to this ticket now, or after the tickets ahead of it. `refusal` is the 409's message when the
        ticket will not wait: a string, or a function of the running ticket. Raises WorkerBusy or WorkerCancelled."""
        wait = parse_wait(wait_seconds)
        with self._cond:
            if self._running is None and not self._waiting:
                self._start(ticket)
                return ticket
            if not wait:
                raise WorkerBusy(refusal(self._running) if callable(refusal) else refusal)
            if len(self._waiting) >= MAX_WAITING:
                raise WorkerBusy(f"{MAX_WAITING} requests are already waiting for the model worker. Try again later.")
            ticket.queued_at = time.time()
            ticket.order = (PRIORITIES[ticket.priority], next(self._count))
            self._waiting.append(ticket)
            self._waiting.sort(key=lambda item: item.order)
            deadline = time.monotonic() + wait
            try:
                while True:
                    if ticket.cancelled:
                        raise WorkerCancelled("Cancelled while it waited; it did not run.")
                    if ticket.alive is not None and not ticket.alive():
                        ticket.cancelled = True
                        raise WorkerCancelled("The caller went away while it waited; it did not run.")
                    if self._running is None and self._waiting[0] is ticket:
                        self._waiting.pop(0)
                        self._start(ticket)
                        return ticket
                    left = deadline - time.monotonic()
                    if left <= 0:
                        ahead = self._waiting.index(ticket) + 1
                        raise WorkerBusy(
                            f"Waited {wait:g} s, and the model worker is still busy with "
                            f"{ahead} request{'s' if ahead != 1 else ''} ahead of this one."
                        )
                    self._cond.wait(min(left, 0.25))
            except BaseException:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                self._cond.notify_all()
                raise

    def _start(self, ticket):
        ticket.started_at = time.time()
        self._running = ticket

    def finish(self, ticket):
        with self._cond:
            if self._running is ticket:
                self._running = None
            ticket.stop = None
            self._cond.notify_all()

    def cancel(self, request_id):
        """Take a waiting request out of the line ("waiting"), or stop a running one ("running"). Raises KeyError when no
        request has that id, and ValueError for a running one that cannot be stopped, such as an install."""
        if not isinstance(request_id, str) or not request_id:
            raise KeyError("No request with that id is waiting or running.")
        with self._cond:
            for ticket in self._waiting:
                if ticket.id == request_id:
                    ticket.cancelled = True
                    self._cond.notify_all()
                    return "waiting"
            ticket = self._running
            if ticket is None or ticket.id != request_id:
                raise KeyError("No request with that id is waiting or running.")
            if not ticket.can_stop or ticket.stop is None:
                raise ValueError(f"{ticket.what} cannot be stopped.")
            ticket.cancelled = True
            stop = ticket.stop
        stop()  # outside the lock: ending a process can take a moment
        return "running"

    def snapshot(self):
        now = time.time()
        with self._cond:
            running = self._running.describe(now) if self._running is not None else None
            waiting = [ticket.describe(now) for ticket in self._waiting]
        return {"running": running, "waiting": waiting, "max_waiting": MAX_WAITING}


def parse_wait(value):
    """wait_seconds as a number of seconds, at most MAX_WAIT_SECONDS; ValueError for anything else."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0:
        raise ValueError("wait_seconds must be a number of seconds, 0 or more.")
    return min(float(value), MAX_WAIT_SECONDS)
