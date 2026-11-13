"""The line for the model worker (job_queue.py): order, priorities, the limits, and cancelling while waiting or running."""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import job_queue  # noqa: E402
from job_queue import JobQueue, Ticket, WorkerBusy, parse_wait  # noqa: E402
from worker_process import WorkerCancelled  # noqa: E402


class Waiter:
    """Takes a ticket in a thread of its own and remembers how it went."""

    def __init__(self, queue, ticket, wait=30, order=None):
        self.queue, self.ticket, self.result, self.order = queue, ticket, None, order
        self.thread = threading.Thread(target=self.run, args=(wait,), daemon=True)
        self.thread.start()

    def run(self, wait):
        try:
            self.queue.take(self.ticket, wait)
        except Exception as exc:  # noqa: BLE001 - the test reads it
            self.result = exc
            return
        self.result = "ran"
        if self.order is not None:
            self.order.append(self.ticket.id)
        self.queue.finish(self.ticket)


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = JobQueue()
        self.first = Ticket("first", "Take", engine="kokoro")
        self.queue.take(self.first)

    def waiting_ids(self):
        return [item["id"] for item in self.queue.snapshot()["waiting"]]

    def test_without_a_wait_a_busy_worker_is_refused_at_once(self):
        started = time.monotonic()
        with self.assertRaisesRegex(WorkerBusy, "^Busy with Take$"):
            self.queue.take(Ticket("second"), 0, refusal=lambda running: "Busy with " + running.what)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.waiting_ids(), [])

    def test_people_go_ahead_of_batch_rows_and_each_keeps_its_order(self):
        order = []
        waiters = []
        for name, priority in (
            ("row-1", "low"),
            ("row-2", "low"),
            ("person-1", "normal"),
            ("row-3", "low"),
            ("person-2", "normal"),
        ):
            waiters.append(Waiter(self.queue, Ticket(name, priority=priority), order=order))
            self.assertTrue(wait_until(lambda n=len(waiters): len(self.waiting_ids()) == n))
        self.assertEqual(self.waiting_ids(), ["person-1", "person-2", "row-1", "row-2", "row-3"])
        self.queue.finish(self.first)
        for waiter in waiters:
            waiter.thread.join(5)
        self.assertEqual(order, ["person-1", "person-2", "row-1", "row-2", "row-3"])
        self.assertFalse(self.queue.busy)

    def test_at_most_twenty_wait(self):
        waiters = [Waiter(self.queue, Ticket(f"w{n}")) for n in range(job_queue.MAX_WAITING)]
        self.assertTrue(wait_until(lambda: len(self.waiting_ids()) == job_queue.MAX_WAITING))
        with self.assertRaisesRegex(WorkerBusy, "20 requests are already waiting"):
            self.queue.take(Ticket("one-too-many"), 30)
        self.queue.finish(self.first)
        for waiter in waiters:
            waiter.thread.join(5)
        self.assertEqual({waiter.result for waiter in waiters}, {"ran"})

    def test_a_wait_that_runs_out_is_refused_and_leaves_the_line(self):
        started = time.monotonic()
        with self.assertRaisesRegex(WorkerBusy, "^Waited 0.3 s, and the model worker is still busy"):
            self.queue.take(Ticket("impatient"), 0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(self.waiting_ids(), [])

    def test_a_waiting_request_can_be_cancelled_and_one_whose_caller_left_goes(self):
        cancelled = Waiter(self.queue, Ticket("cancel-me"))
        present = {"value": True}
        gone = Waiter(self.queue, Ticket("caller-leaves", alive=lambda: present["value"]))
        self.assertTrue(wait_until(lambda: len(self.waiting_ids()) == 2))
        self.assertEqual(self.queue.cancel("cancel-me"), "waiting")
        cancelled.thread.join(5)
        self.assertIsInstance(cancelled.result, WorkerCancelled)
        self.assertIn("did not run", str(cancelled.result))
        present["value"] = False
        gone.thread.join(5)
        self.assertIsInstance(gone.result, WorkerCancelled)
        self.assertEqual(self.waiting_ids(), [])
        self.assertIs(self.queue.running, self.first)

    def test_a_running_request_is_stopped_through_its_owner_and_some_cannot_be(self):
        with self.assertRaises(ValueError):
            self.queue.cancel("first")  # its owner gave it no stop
        stopped = []
        self.first.stop = lambda: stopped.append(True)
        self.assertEqual(self.queue.cancel("first"), "running")
        self.assertEqual((stopped, self.first.cancelled), ([True], True))
        with self.assertRaises(KeyError):
            self.queue.cancel("nobody")
        install = Ticket("install", "Install", can_stop=False)
        install.stop = lambda: None
        self.queue.finish(self.first)
        self.queue.take(install)
        with self.assertRaisesRegex(ValueError, "^Install cannot be stopped"):
            self.queue.cancel("install")

    def test_the_snapshot_says_who_runs_and_who_waits(self):
        Waiter(self.queue, Ticket("row", "Take for a scene batch", priority="low", engine="kokoro"))
        self.assertTrue(wait_until(lambda: self.waiting_ids() == ["row"]))
        self.first.stop = lambda: None
        snapshot = self.queue.snapshot()
        self.assertEqual(
            {key: snapshot["running"][key] for key in ("id", "what", "engine", "priority", "can_stop", "detail")},
            {"id": "first", "what": "Take", "engine": "kokoro", "priority": "normal", "can_stop": True, "detail": None},
        )
        self.assertEqual(snapshot["waiting"][0]["priority"], "low")
        self.assertEqual(snapshot["max_waiting"], 20)
        self.queue.cancel("row")

    def test_wait_seconds_must_be_a_number(self):
        self.assertEqual((parse_wait(None), parse_wait(0), parse_wait(2.5), parse_wait(10**6)), (0, 0, 2.5, 3600))
        for bad in (-1, "10", True, float("nan")):
            with self.assertRaises(ValueError):
                parse_wait(bad)
        with self.assertRaises(ValueError):
            Ticket("x", priority="urgent")


if __name__ == "__main__":
    unittest.main()
