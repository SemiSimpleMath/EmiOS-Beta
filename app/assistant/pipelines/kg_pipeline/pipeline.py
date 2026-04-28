"""
KGPipeline — the new bucket-aware KG ingest pipeline.

Six stages, each with its own daemon thread and its own output table.
See docs/architecture/09_KG_PIPELINE.md for the full design.

Stage 5 (proposal_promoter) is a separate scheduled routine — not part of
this pipeline.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.kg_pipeline.runner import KGPipelineRunner
from app.assistant.pipelines.kg_pipeline.steps import (
    CritiqueAndExtractStep,
    EnrichExtractionStep,
    ResolveMessagesStep,
    SegmentMessagesStep,
    WriteProposalsStep,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class KGPipeline:
    """The new KG ingest pipeline (2026-04-25 refactor)."""

    pipeline_id = "kg_pipeline"

    def __init__(
        self,
        *,
        poll_interval: float = 5.0,
        db_lock_backoff: float = 3.0,
    ) -> None:
        self._runner = KGPipelineRunner(
            steps=[
                ResolveMessagesStep(),        # Stage 1 (also does chat-eligibility filter)
                SegmentMessagesStep(),        # Stage 2
                CritiqueAndExtractStep(),     # Stage 3
                EnrichExtractionStep(),       # Stage 3.5
                WriteProposalsStep(),         # Stage 4
            ],
            poll_interval=poll_interval,
            db_lock_backoff=db_lock_backoff,
        )

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        run_id: Optional[str] = None,
        max_idle_rounds: int = 5,
        monitor_interval: float = 15.0,
        only_steps: Optional[list] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        # Subsystem flag check (kept under existing name for UI continuity).
        from app.assistant.utils.subsystem_flags import is_subsystem_enabled
        if not is_subsystem_enabled("kg_chat_pipeline"):
            logger.info("[kg_pipeline] skipped — subsystem flag kg_chat_pipeline is disabled")
            return {
                "status": "skipped",
                "reason": "subsystem_disabled",
                "pipeline_id": self.pipeline_id,
            }

        ctx = PipelineContext.for_date(
            pipeline_id=self.pipeline_id,
            target_date=target_date,
            run_id=run_id,
        )
        result = self._runner.run(
            ctx,
            max_idle_rounds=max_idle_rounds,
            monitor_interval=monitor_interval,
        )
        return {
            "pipeline_id": result.pipeline_id,
            "run_id": result.run_id,
            "date": result.date,
            "status": result.status,
            "workers": result.workers,
        }

    def stop(self) -> None:
        self._runner.stop()

    def get_live_stats(self) -> Dict[str, Any]:
        return self._runner.get_live_stats()
