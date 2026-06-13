"""Exclusive cross-process writer lock for the chroma index.

chroma's PersistentClient is NOT multi-process safe: two OS processes
opening the same index files concurrently corrupt the hnsw segments.
All three corruptions on 2026-06-12 were multi-process (an external
backfill script, a pytest run, validation scripts) racing the server.
In-process multi-thread access IS safe — chroma's rust core serializes
it internally, verified by a 15k-op adversarial stress test — so the
ONLY thing we must prevent is a second process.

This is an advisory OS file lock on ``<chroma_dir>/.chroma_writer.lock``.
The first process to open chroma holds it for its lifetime; a second
process that tries to open chroma fails LOUDLY (ChromaWriterLockError)
with the owner's PID instead of silently corrupting the index. OS file
locks release on process death, so a crashed server leaves no stale lock
to clear.

Tests use fakes and never touch the real index (USE_TEST_DB), so the
lock is a no-op there.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_LOCK_FILENAME = ".chroma_writer.lock"
_OWNER_FILENAME = ".chroma_writer.owner"

# Held for the process lifetime — keep the handle referenced so it isn't
# garbage-collected (closing it would release the OS lock).
_held_handle = None
_held_path = None


class ChromaWriterLockError(RuntimeError):
    """Raised when another live process already owns the chroma writer lock."""


def _skip() -> bool:
    return os.environ.get("USE_TEST_DB", "").strip().lower() in ("1", "true", "yes")


def _owner_pid(owner_path: Path) -> str:
    try:
        return owner_path.read_text(encoding="utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


def acquire_chroma_writer_lock(chroma_dir) -> None:
    """Acquire the exclusive cross-process chroma lock.

    Idempotent within a process (re-call is a no-op). Raises
    ChromaWriterLockError if another LIVE process holds it — that is the
    corruption vector, surfaced loudly. A non-contention failure (can't
    create/open the lock file, locking unsupported) logs and proceeds
    WITHOUT the guard rather than bricking boot over the safety helper.
    """
    global _held_handle, _held_path
    if _skip() or _held_handle is not None:
        return

    try:
        d = Path(chroma_dir)
        d.mkdir(parents=True, exist_ok=True)
        lock_path = d / _LOCK_FILENAME
        owner_path = d / _OWNER_FILENAME
        fh = open(lock_path, "a+")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write("lock")
            fh.flush()
    except Exception as e:
        logger.error("[chroma_lock] could not open lock file (%s) — proceeding "
                     "WITHOUT cross-process guard", e)
        return

    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Held by another LIVE process — fail loud (the whole point).
        owner = _owner_pid(d / _OWNER_FILENAME)
        try:
            fh.close()
        except Exception:
            pass
        raise ChromaWriterLockError(
            f"chroma index is already open by another process (PID {owner}). "
            f"The running server is the ONLY chroma writer — a second process must "
            f"not open chroma while it runs. Stop the server first, or query it "
            f"through the server instead of opening the index directly. "
            f"Lock file: {lock_path}"
        )
    except Exception as e:
        logger.error("[chroma_lock] unexpected lock error (%s) — proceeding "
                     "WITHOUT cross-process guard", e)
        try:
            fh.close()
        except Exception:
            pass
        return

    _held_handle = fh
    _held_path = str(lock_path)
    try:
        owner_path.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    logger.info("[chroma_lock] writer lock acquired (pid=%s) at %s",
                os.getpid(), lock_path)


def release_chroma_writer_lock() -> None:
    """Release the lock (process exit releases it anyway; explicit release
    is for tests and clean shutdown)."""
    global _held_handle, _held_path
    if _held_handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            _held_handle.seek(0)
            msvcrt.locking(_held_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_held_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _held_handle.close()
    except Exception:
        pass
    _held_handle = None
    _held_path = None
