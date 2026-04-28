from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.routine_manager.utils import write_json_file, utc_now
from app.assistant.utils.logging_config import get_logger

from .step_types import PipelineStep, StepResult, outputs_exist

logger = get_logger(__name__)


def _prune_pipeline_runs_dir(*, pipeline_runs_dir: Path, max_files: int = 1500) -> None:
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


@dataclass
class PipelineRunResult:
    pipeline_id: str
    run_id: str
    date: str
    started_at_utc: str
    finished_at_utc: str
    status: str
    steps: List[Dict[str, Any]]


class PipelineRunner:
    """
    Generic sequential runner for step-based pipelines.

    Requirements on ctx:
    - ctx.pipeline_id, ctx.run_id, ctx.date_str
    - ctx.day_dir, ctx.pipeline_runs_dir
    - ctx.audit_path() -> Path
    """

    def __init__(self, *, steps: List[PipelineStep]):
        self.steps = steps

    def run(
        self,
        ctx: Any,
        *,
        only_steps: Optional[list[str]] = None,
        force: bool = False,
    ) -> PipelineRunResult:
        ctx.day_dir.mkdir(parents=True, exist_ok=True)
        ctx.pipeline_runs_dir.mkdir(parents=True, exist_ok=True)

        started = utc_now().isoformat()
        wanted = set([s.strip() for s in (only_steps or []) if str(s).strip()]) if only_steps else None

        step_dicts: List[Dict[str, Any]] = []
        overall_status = "success"

        for step in self.steps:
            if wanted is not None and step.name not in wanted:
                continue

            # Idempotency default: skip if all outputs exist unless force=True.
            if not force and outputs_exist(step, ctx):
                step_dicts.append(
                    StepResult(
                        name=step.name,
                        status="skipped",
                        duration_s=0.0,
                        inputs=step.inputs(ctx),
                        outputs=step.outputs(ctx),
                    ).to_dict()
                )
                continue

            t0 = time.monotonic()
            try:
                logger.info("[pipeline:%s] >> %s", ctx.run_id, step.name)
                step.run(ctx)
                elapsed = time.monotonic() - t0
                step_dicts.append(
                    StepResult(
                        name=step.name,
                        status="success",
                        duration_s=elapsed,
                        inputs=step.inputs(ctx),
                        outputs=step.outputs(ctx),
                    ).to_dict()
                )
                logger.info("[pipeline:%s] << %s OK (%.1fs)", ctx.run_id, step.name, elapsed)
            except Exception as e:
                elapsed = time.monotonic() - t0
                overall_status = "error"
                step_dicts.append(
                    StepResult(
                        name=step.name,
                        status="error",
                        duration_s=elapsed,
                        error=str(e)[:800],
                        inputs=step.inputs(ctx),
                        outputs=step.outputs(ctx),
                    ).to_dict()
                )
                logger.error("[pipeline:%s] << %s FAILED (%.1fs)", ctx.run_id, step.name, elapsed)
                logger.debug("[pipeline:%s] << %s FAILED (%.1fs) exception details", ctx.run_id, step.name, elapsed, exc_info=True)
                break

        finished = utc_now().isoformat()
        result = PipelineRunResult(
            pipeline_id=ctx.pipeline_id,
            run_id=ctx.run_id,
            date=ctx.date_str,
            started_at_utc=started,
            finished_at_utc=finished,
            status=overall_status,
            steps=step_dicts,
        )

        # Always write audit file.
        write_json_file(
            ctx.audit_path(),
            {
                "schema_version": 1,
                "pipeline_id": result.pipeline_id,
                "run_id": result.run_id,
                "date": result.date,
                "started_at_utc": result.started_at_utc,
                "finished_at_utc": result.finished_at_utc,
                "status": result.status,
                "steps": result.steps,
            },
        )

        # Retain only the most recent audits for this day.
        try:
            _prune_pipeline_runs_dir(pipeline_runs_dir=ctx.pipeline_runs_dir, max_files=1500)
        except Exception:
            pass
        return result

