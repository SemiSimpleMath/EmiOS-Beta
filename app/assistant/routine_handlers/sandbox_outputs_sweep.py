"""Routine handler — sweep stale binary-output files from sandbox executions.

Binary outputs from `execute_code` are copied to data/sandbox_outputs/<call_id>/
so output pods can carry a real file via metadata.stored_path (same convention
as image/document/audio/video pods). The pods themselves stay in pod_store as
historical records; the on-disk files only need to live long enough for any
downstream tool call (send_email attach, pod_fetch, etc.) to consume them.

This sweep drops files older than the configured age and removes empty
call_id subdirs left behind. Default 7 days — enough for "the email landed
two days ago and the user circled back to forward it" use cases without
letting binary outputs accumulate indefinitely.

Corresponding routine entry at:
    configs/routines/public/sandbox_outputs_sweep.json
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


@routine_handler(name="sandbox_outputs_sweep")
def sandbox_outputs_sweep(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Drop files (and empty call_id subdirs) under data/sandbox_outputs/
    older than `max_age_days` (default 7)."""
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    max_age_days = float(spec.get("max_age_days", 7))
    cutoff = time.time() - (max_age_days * 86400.0)

    from app.assistant.utils.path_utils import get_data_dir
    root = get_data_dir() / "data" / "sandbox_outputs"
    if not root.exists():
        return {"status": "ok", "files_removed": 0, "dirs_removed": 0}

    files_removed = 0
    bytes_freed = 0
    for call_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in call_dir.iterdir():
            if not f.is_file():
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    size = f.stat().st_size
                    f.unlink()
                    files_removed += 1
                    bytes_freed += size
            except OSError as e:
                logger.warning("sandbox_outputs_sweep: failed to remove %s: %s", f, e)

    dirs_removed = 0
    for call_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            next(call_dir.iterdir())
        except StopIteration:
            try:
                call_dir.rmdir()
                dirs_removed += 1
            except OSError as e:
                logger.warning("sandbox_outputs_sweep: failed to remove dir %s: %s", call_dir, e)

    logger.info(
        "sandbox_outputs_sweep: removed %d files (%.1f MB), %d empty dirs (max_age_days=%.1f)",
        files_removed, bytes_freed / 1_048_576, dirs_removed, max_age_days,
    )
    return {
        "status": "ok",
        "files_removed": files_removed,
        "bytes_freed": bytes_freed,
        "dirs_removed": dirs_removed,
        "max_age_days": max_age_days,
    }
