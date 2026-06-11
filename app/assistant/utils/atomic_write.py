"""Atomic file writes — the ONE temp-file + fsync + os.replace implementation.

Consolidates eight near-identical copies found by the 2026-06-10 duplicate
audit (dayflow stages, situation_brief, step_types, routine_manager). Only
the routine_manager copy carried the Windows hardening: ``os.replace`` can
fail with PermissionError when another thread momentarily holds the target
file open, so the replace retries with a short backoff. That behavior is
now universal.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def _replace_with_retry(tmp_path: str, path: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(tmp_path, str(path))
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_text_atomic(path: Path, text: str) -> None:
    """Write text to ``path`` atomically (temp file, fsync, replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as pretty JSON to ``path`` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
