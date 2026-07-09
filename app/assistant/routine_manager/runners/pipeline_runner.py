from __future__ import annotations

from app.assistant.pipelines.pipeline_registry import resolve_pipeline

from .types import RoutineLike
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult


class PipelineRoutineRunner:
    def run(self, routine: RoutineLike, run_ctx: RoutineRunContext) -> RoutineRunResult:
        spec = routine.spec or {}
        pipeline_id = str(spec.get("pipeline_id") or "").strip()
        if not pipeline_id:
            raise ValueError("pipeline runner requires spec.pipeline_id")

        only_steps = spec.get("only_steps")
        if only_steps is not None and not isinstance(only_steps, list):
            raise ValueError("spec.only_steps must be a list of step names")

        pipeline = resolve_pipeline(pipeline_id)
        if pipeline is None:
            raise ValueError(f"Unknown pipeline_id: {pipeline_id}")

        result = pipeline.run(
            target_date=run_ctx.target_date,
            only_steps=only_steps,
            run_id=run_ctx.run_id,
            force=run_ctx.force,
        )
        # The pipeline contract is a dict with an explicit top-level
        # "status" (success|error, plus soft statuses like "skipped").
        # Fail loud on a missing key rather than guess — this status now
        # drives the routine failure machinery (backoff/auto-disable).
        if not isinstance(result, dict):
            raise RuntimeError(
                f"Pipeline '{pipeline_id}' returned {type(result).__name__}; "
                "the pipeline contract is a dict with a 'status' key"
            )
        pipeline_status = str(result.get("status") or "").strip().lower()
        if not pipeline_status:
            raise RuntimeError(
                f"Pipeline '{pipeline_id}' returned no 'status' key; "
                "return status='success'|'error' like the other pipelines"
            )
        message = ""
        if pipeline_status not in ("success", "skipped"):
            failed = result.get("failed_steps")
            message = f"pipeline status={pipeline_status}" + (
                f" failed_steps={failed}" if failed else ""
            )
        return RoutineRunResult(
            status=pipeline_status,
            message=message,
            data={"pipeline_id": pipeline_id, "pipeline_result": result},
        )
