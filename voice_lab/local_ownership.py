"""Process-scoped local ownership released by the OS when a service exits."""

import errno
import os
import threading
from pathlib import Path


class LocalOwnership:
    """Nonblocking exclusive byte lock; keep the file, never use its existence as a lease."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self._file = None
        self._guard = threading.RLock()

    @property
    def held(self):
        with self._guard:
            return self._file is not None

    def acquire(self):
        with self._guard:
            if self._file is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    if handle.seek(0, 2) == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            self._file = handle
            return True

    def release(self):
        with self._guard:
            if self._file is None:
                return
            handle, self._file = self._file, None
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
