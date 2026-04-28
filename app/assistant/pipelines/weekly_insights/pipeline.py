from __future__ import annotations

from typing import Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.pipelines.scope_policy import load_pipeline_scope_policy
from app.assistant.pipelines.step_runner import PipelineRunner

from .weekly_context import WeeklyPipelineContext
from .steps import CollectDailySummariesStep, SynthesizeWeeklyInsightsStep

logger = get_logger(__name__)


class WeeklyInsightsPipeline:
    pipeline_id = "weekly_insights"

    def __init__(self) -> None:
        self._scope_policy = load_pipeline_scope_policy(self.pipeline_id)
        self._runner = PipelineRunner(
            steps=[
                CollectDailySummariesStep(),
                SynthesizeWeeklyInsightsStep(),
            ]
        )

    def run(
        self,
        *,
        end_date: Optional[str] = None,
        target_date: Optional[str] = None,
        num_days: int = 7,
        only_steps: Optional[list[str]] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict:
        # target_date is the standard PipelineRoutineRunner kwarg — treat as end_date
        resolved_end = end_date or target_date
        ctx = WeeklyPipelineContext.for_week_ending(
            pipeline_id=self.pipeline_id,
            end_date_str=resolved_end,
            num_days=num_days,
            run_id=run_id,
        )
        result = self._runner.run(ctx, only_steps=only_steps, force=force)
        return {
            "pipeline_id": result.pipeline_id,
            "run_id": result.run_id,
            "week_end": ctx.end_date_str,
            "status": result.status,
            "audit_path": str(ctx.audit_path()),
            "steps": result.steps,
        }
