"""Exclusive cross-process chroma writer lock (2026-06-12).

The proven corruption vector is two OS processes opening the index at
once. The lock makes the second process fail loudly. Verified for real
with a holder subprocess — a unit-level mock wouldn't exercise the OS
lock, which is the whole feature.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

import app.assistant.kg.chroma.chroma_lock as cl
from app.assistant.utils.path_utils import get_repo_root


def test_skips_under_test_db(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_TEST_DB", "true")
    cl.release_chroma_writer_lock()
    cl.acquire_chroma_writer_lock(tmp_path)  # no-op under test DB
    assert cl._held_handle is None


def test_idempotent_within_process(monkeypatch, tmp_path):
    monkeypatch.delenv("USE_TEST_DB", raising=False)
    cl.release_chroma_writer_lock()
    try:
        cl.acquire_chroma_writer_lock(tmp_path)
        handle = cl._held_handle
        assert handle is not None
        cl.acquire_chroma_writer_lock(tmp_path)  # re-call: same handle, no error
        assert cl._held_handle is handle
    finally:
        cl.release_chroma_writer_lock()


def test_second_process_is_refused(monkeypatch, tmp_path):
    """A holder subprocess takes the lock; this process must be refused."""
    lockdir = tmp_path / "chroma"
    ready = tmp_path / "ready.txt"
    holder_py = tmp_path / "holder.py"
    holder_py.write_text(
        "import sys, os, time\n"
        f"sys.path.insert(0, r'{get_repo_root()}')\n"
        "os.environ.pop('USE_TEST_DB', None)\n"
        "from app.assistant.kg.chroma.chroma_lock import acquire_chroma_writer_lock\n"
        f"acquire_chroma_writer_lock(r'{lockdir}')\n"
        f"open(r'{ready}', 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, str(holder_py)])
    try:
        # Wait for the holder to actually acquire (it writes its PID then sleeps).
        for _ in range(150):
            if ready.exists():
                break
            time.sleep(0.1)
        assert ready.exists(), "holder subprocess never acquired the lock"

        # This process tries the same dir — bypass the test-DB skip first.
        monkeypatch.delenv("USE_TEST_DB", raising=False)
        cl.release_chroma_writer_lock()
        with pytest.raises(cl.ChromaWriterLockError) as exc:
            cl.acquire_chroma_writer_lock(lockdir)
        # The error names the holder's PID for diagnosis.
        assert ready.read_text().strip() in str(exc.value)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        cl.release_chroma_writer_lock()


def test_lock_frees_on_holder_exit(monkeypatch, tmp_path):
    """OS releases the lock when the holder dies — no stale lock to clear."""
    lockdir = tmp_path / "chroma"
    ready = tmp_path / "ready.txt"
    holder_py = tmp_path / "holder.py"
    holder_py.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, r'{get_repo_root()}')\n"
        "os.environ.pop('USE_TEST_DB', None)\n"
        "from app.assistant.kg.chroma.chroma_lock import acquire_chroma_writer_lock\n"
        f"acquire_chroma_writer_lock(r'{lockdir}')\n"
        f"open(r'{ready}', 'w').write('ok')\n"
    )
    proc = subprocess.run([sys.executable, str(holder_py)], timeout=30)
    assert ready.exists() and proc.returncode == 0
    # Holder has exited -> lock is free -> we can take it.
    monkeypatch.delenv("USE_TEST_DB", raising=False)
    cl.release_chroma_writer_lock()
    try:
        cl.acquire_chroma_writer_lock(lockdir)
        assert cl._held_handle is not None
    finally:
        cl.release_chroma_writer_lock()
