"""Single-instance guard.

Scheduling is external, so nothing stops cron from firing a second run while a
slow one is still going. An advisory lock on a file makes the second run bow out
instead of double-processing the mailbox. The OS releases the lock when the
process dies, including on SIGKILL, so a crashed run cannot wedge the schedule.
"""

from __future__ import annotations

import errno
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

log = logging.getLogger(__name__)

DEFAULT_LOCK_NAME = "imap-to-papra.lock"


class AlreadyRunning(Exception):
    """Another instance currently holds the lock."""


def default_lock_path() -> Path:
    return Path(tempfile.gettempdir()) / DEFAULT_LOCK_NAME


def _try_lock(handle: IO[str]) -> bool:
    """Take a non-blocking exclusive lock. True if acquired."""
    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    try:
        import msvcrt
    except ImportError:  # pragma: no cover - no locking primitive available
        log.debug("no file locking primitive on this platform; skipping overlap guard")
        return True

    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _open_lock_file(path: Path) -> IO[str]:
    """Open the lock file for read/write, refusing to follow a symlink.

    The default path lives in a world-writable temp directory, so without
    O_NOFOLLOW anyone able to plant a symlink there could redirect the truncate
    and PID write below onto a file of their choosing.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, "r+", encoding="utf-8")


@contextmanager
def single_instance(path: Path | None = None) -> Iterator[Path]:
    """Hold an exclusive lock for the duration of the block.

    Raises AlreadyRunning if another process got there first.
    """
    lock_path = path or default_lock_path()

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = _open_lock_file(lock_path)
    except OSError as exc:
        # A lock we cannot open should not stop the actual work.
        if exc.errno == errno.ELOOP:
            log.warning("lock file %s is a symlink and was not followed; continuing without an overlap guard", lock_path)
        else:
            log.warning("cannot create lock file %s (%s); continuing without an overlap guard", lock_path, exc)
        yield lock_path
        return

    try:
        if not _try_lock(handle):
            raise AlreadyRunning(str(lock_path))

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()

        yield lock_path
    finally:
        handle.close()
