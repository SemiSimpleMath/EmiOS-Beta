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
        pipeline_status = result.get("status", "error") if isinstance(result, dict) else "error"
        return RoutineRunResult(status=pipeline_status, data={"pipeline_id": pipeline_id, "pipeline_result": result})
