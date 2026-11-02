"""
Single-worker lock for the cron queue (brief section 7).

Why this exists
---------------
The cron contract is "run for at most 50 seconds", but a 2 GB import can exceed
that. Without a lock the next cron run starts while the previous one is still
downloading; both then touch the same temp directory and the first worker's
``.part`` file can be deleted underneath it, which surfaces as
``missing_source`` in the job log.

Implementation is deliberately portable: an exclusive-create lock file, with a
staleness timeout so a crashed worker cannot block the queue forever.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from django.conf import settings

DEFAULT_STALE_SECONDS = 30 * 60


class WorkerBusy(RuntimeError):
    """Another worker holds the lock."""


def lock_path() -> Path:
    root = Path(settings.TEMP_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root / "worker.lock"


def _stale_seconds() -> int:
    return int(getattr(settings, "WORKER_LOCK_STALE_SECONDS", DEFAULT_STALE_SECONDS))


def _is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > _stale_seconds()


def _owner_alive(path: Path) -> bool:
    """
    True when the PID recorded in the lock file is still running.

    A killed worker leaves a fresh-looking lock; checking the PID lets the next
    cron run continue immediately instead of waiting for the staleness timeout.
    """
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
        pid = int(first)
    except (OSError, IndexError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user: treat as alive.
        return True
    except OSError as exc:
        # Windows reports a missing PID as a generic OSError (WinError 87).
        if getattr(exc, "winerror", None) in (87, 1003):
            return False
        if getattr(exc, "errno", None) == 3:  # ESRCH
            return False
        return True
    return True


@contextlib.contextmanager
def worker_lock():
    """
    Hold the queue lock for the duration of the block.

    Raises :class:`WorkerBusy` if another live worker owns it. A lock file older
    than the staleness timeout is treated as abandoned and taken over.
    """
    path = lock_path()

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _owner_alive(path) and not _is_stale(path):
            raise WorkerBusy(f"Another worker is running (lock {path.name}).") from None
        # Abandoned lock: take it over.
        try:
            path.unlink()
        except OSError:
            raise WorkerBusy("Worker lock is stale but could not be removed.") from None
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    try:
        os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode())
        os.close(fd)
        yield path
    finally:
        with contextlib.suppress(OSError):
            path.unlink()
