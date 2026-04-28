from __future__ import annotations

from typing import Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.pipelines.scope_policy import load_pipeline_scope_policy

from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.step_runner import PipelineRunner
from .steps import (
    ArchiveDailyAssessmentStep,
    ArchiveDailyAssessmentSummaryStep,
    ArchiveDailyContextStep,
    ArchiveDailyInsightsStep,
    ArchiveDailyTicketsStep,
    ArchiveDailyTimelineStep,
)

logger = get_logger(__name__)


class DailyInsightsPipeline:
    pipeline_id = "daily_insights"

    def __init__(self) -> None:
        self._scope_policy = load_pipeline_scope_policy(self.pipeline_id)
        self._runner = PipelineRunner(
            steps=[
                ArchiveDailyContextStep(),
                ArchiveDailyTicketsStep(),
                ArchiveDailyTimelineStep(),
                ArchiveDailyInsightsStep(),
                ArchiveDailyAssessmentStep(),
                ArchiveDailyAssessmentSummaryStep(),
            ]
        )

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        only_steps: Optional[list[str]] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict:
        ctx = PipelineContext.for_date(pipeline_id=self.pipeline_id, target_date=target_date, run_id=run_id)
        result = self._runner.run(ctx, only_steps=only_steps, force=force)
        return {
            "pipeline_id": result.pipeline_id,
            "run_id": result.run_id,
            "date": result.date,
            "status": result.status,
            "audit_path": str(ctx.audit_path()),
            "steps": result.steps,
        }

