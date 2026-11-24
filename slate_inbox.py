"""The Lab's inbox in Fel's Studio (suite contract 6): the .voiceslate files Voice Slate hands over, waiting for the person.

fels_suite.py lists the folder and clears a file from it. This module checks each waiting file the way the Cast page checks
a file picked by hand (slate_bundle.read_bundle), once for as long as the file is unchanged, and reads a file again for its
import. Nothing is imported without the person's yes; after an import or a Dismiss the file goes to the Recycle Bin."""

import os
import threading
import time

import fels_suite
from recorder_clip import ClipRefused
from slate_bundle import MAX_BUNDLE_BYTES, read_bundle

APP = "voice-lab"
KINDS = (".voiceslate",)
# One listing checks new files for at most this long. The rest answer "checking" and are checked by the next listing, so a
# full inbox never holds up the page.
CHECK_SECONDS = 1.5


class NotWaiting(KeyError):
    """The file is not, or no longer, in the inbox. Answered as 404."""


class NotCleared(RuntimeError):
    """The file could not be sent to the Recycle Bin."""

    status_code = 500


def problem_sentence(exc):
    """A refusal of the clip check as sentences: its reasons start in lower case and run together with "; "."""
    problems = getattr(exc, "problems", None) or [str(exc)]
    return " ".join(p[:1].upper() + p[1:] + ("" if p.endswith(".") else ".") for p in problems if p)


class SlateInbox:
    """What waits in inbox/voice-lab/. `environ` names the suite folder as fels_suite reads it (the process's own by default),
    and `discard` replaces the Recycle Bin, for tests."""

    def __init__(self, environ=None, *, discard=None, clock=time.monotonic):
        self.environ = environ
        self.discard = discard
        self._clock = clock
        self._checked = {}  # file name -> ((size, mtime_ns), the answer of its check)
        self._lock = threading.Lock()

    def folder(self):
        return fels_suite.inbox_dir(APP, self.environ)

    def _entries(self):
        return fels_suite.inbox_list(APP, KINDS, MAX_BUNDLE_BYTES, self.environ)

    def count(self):
        """How many files wait, checked or not."""
        return len(self._entries())

    def waiting(self):
        """Every waiting file, oldest first: name, sender, sender_name, sent, size and state. A state is "ready" with `clip`
        (the same summary the file box's preview shows), "checking" (not checked yet), or "refused" with `code` ("kind",
        "size", "clip" or "unreadable") and `problem`, a sentence. A refused file can only be dismissed."""
        entries = self._entries()
        deadline = self._clock() + CHECK_SECONDS
        files = []
        with self._lock:
            for entry in entries:
                item = {key: entry[key] for key in ("name", "sender", "sender_name", "sent", "size")}
                if entry["code"]:
                    item.update(state="refused", code=entry["code"], problem=entry["problem"])
                else:
                    item.update(self._check(entry, deadline))
                files.append(item)
            names = {entry["name"] for entry in entries}
            for name in [name for name in self._checked if name not in names]:
                del self._checked[name]
        return files

    def _check(self, entry, deadline):
        try:
            stamp = os.stat(entry["path"], follow_symlinks=False)
        except OSError:
            return {"state": "checking"}  # it went while the folder was read; the next listing leaves it out
        key = (stamp.st_size, stamp.st_mtime_ns)
        known = self._checked.get(entry["name"])
        if known and known[0] == key:
            return known[1]
        if self._clock() > deadline:
            return {"state": "checking"}
        try:
            answer = {"state": "ready", "clip": read_bundle(self._read(entry["path"]), accept_asr_draft=True).summary()}
        except ClipRefused as exc:
            answer = {"state": "refused", "code": "clip", "problem": problem_sentence(exc)}
        except OSError:  # another program has it open; not remembered, so the next listing tries again
            return {
                "state": "refused",
                "code": "unreadable",
                "problem": "The file could not be read. Another program may have it open.",
            }
        except Exception as exc:  # a fault in the check itself: one file must never stop the whole list from showing
            answer = {
                "state": "refused",
                "code": "clip",
                "problem": f"Voice Lab could not check this file ({type(exc).__name__}: {exc}). Dismiss it from the list.",
            }
        self._checked[entry["name"]] = (key, answer)
        return answer

    @staticmethod
    def _read(path):
        with open(path, "rb") as source:
            return source.read(MAX_BUNDLE_BYTES + 1)  # read_bundle refuses anything longer

    def find(self, name):
        """The listing entry of a waiting file, by its exact name. NotWaiting when it is not there."""
        for entry in self._entries():
            if entry["name"] == name:
                return entry
        raise NotWaiting(f"{name} is no longer in the inbox. It may have been imported or dismissed already.")

    def read(self, name):
        """(entry, the file's bytes) for an import. A file of another kind or over the limit is a ValueError."""
        entry = self.find(name)
        if entry["code"]:
            raise ValueError(f"{entry['problem']} Dismiss it from the list.")
        return entry, self._read(entry["path"])

    def clear(self, entry):
        """Send a waiting file to the Recycle Bin (deleted, outside Windows). NotCleared when that fails."""
        try:
            fels_suite.inbox_done(entry["path"], self.environ, discard=self.discard)
        except OSError as exc:
            raise NotCleared(f"{exc} Close any program that has it open, then try again.") from exc
        with self._lock:
            self._checked.pop(entry["name"], None)

    def dismiss(self, name):
        self.clear(self.find(name))
