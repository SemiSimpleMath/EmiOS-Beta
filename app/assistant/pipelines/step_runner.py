from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.assistant.pipelines.audit_retention import prune_pipeline_runs_dir
from app.assistant.routine_manager.utils import utc_now
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger

from .step_types import PipelineStep, StepResult, outputs_exist

logger = get_logger(__name__)


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
        write_json_atomic(
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
            prune_pipeline_runs_dir(pipeline_runs_dir=ctx.pipeline_runs_dir, max_files=1500)
        except Exception:
            pass
        return result

