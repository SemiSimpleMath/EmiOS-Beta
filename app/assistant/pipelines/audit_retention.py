"""Retention policy for pipeline run-audit files.

Shared by the generic ``PipelineRunner`` and the ``DayFlowRunner`` — the
two runners are deliberately different machines (one-shot/idempotent vs
continuous-tick/stateful), but their audit retention is the same.
"""
from __future__ import annotations

from pathlib import Path


def prune_pipeline_runs_dir(*, pipeline_runs_dir: Path, max_files: int = 1500) -> None:
    """
    Best-effort retention policy for `day_context/.../pipeline_runs/`.

    Keeps only the most recent N JSON audit files per day-dir.
    """
    try:
        if not pipeline_runs_dir.exists():
            return
        max_files = int(max_files) if int(max_files) > 0 else 1500

        files = [p for p in pipeline_runs_dir.glob("*.json") if p.is_file()]
        if len(files) <= max_files:
            return

        # Keep newest by mtime; drop the rest.
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            try:
                # Avoid deleting any conventional "latest" pointer if introduced later.
                if "latest" in p.name.lower():
                    continue
                p.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception:
        return
