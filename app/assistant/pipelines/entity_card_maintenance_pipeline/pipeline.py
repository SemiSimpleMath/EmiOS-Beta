"""
EntityCardMaintenancePipeline — scheduled scan that writes all entity card
maintenance findings into entity_card_maintenance_finding for human review
via /entity-card-maintenance.

Steps (run in order, all cheap SQL — no LLM):
  1. broken_link_scan    — source_node_id points to deleted KG node  (high priority → deactivate)
  2. junk_name_scan      — obvious artifact entity names              (high priority → deactivate)
  3. blank_content_scan  — empty summary or key_facts                 (medium priority → review)
  4. low_confidence_scan — generator confidence < 0.5                 (medium priority → review)
  5. stale_content_scan  — KG node updated >30 days after card        (low priority → review)
  6. no_link_scan        — source_node_id is NULL                     (low priority → review)

Session / LLM contract
----------------------
No LLM calls in any step.  All steps read data in a short-lived session,
close it, then write via the store (which self-manages its own sessions).
"""
from __future__ import annotations

from typing import Optional

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class EntityCardMaintenancePipeline:
    pipeline_id = "entity_card_maintenance_pipeline"

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        run_id: Optional[str] = None,
        only_steps: Optional[list[str]] = None,
        skip_steps: Optional[list[str]] = None,
        force: bool = False,
    ) -> dict:
        """
        Returns {"run_id": str, "steps": {step_name: step_result | {"error": str}}}.

        Each step runs independently; a failure in one step does not abort others.
        """
        skip = set(skip_steps or [])
        ctx = PipelineContext.for_date(
            pipeline_id=self.pipeline_id,
            target_date=target_date,
            run_id=run_id,
        )
        logger.debug("[EntityCardMaintenancePipeline] Starting run_id=%s", ctx.run_id)
        summary: dict = {"run_id": ctx.run_id, "steps": {}}

        def _should_run(name: str) -> bool:
            if only_steps:
                return name in only_steps
            return name not in skip

        def _run_step(name: str, fn):
            if not _should_run(name):
                return
            try:
                result = fn()
                summary["steps"][name] = result
                logger.debug("[EntityCardMaintenancePipeline] %s: %s", name, result)
            except Exception:
                logger.debug("[EntityCardMaintenancePipeline] %s failed", name, exc_info=True)
                summary["steps"][name] = {"error": "step failed — see logs"}

        _run_step("broken_link_scan", lambda: (
            __import__(
                "app.assistant.pipelines.entity_card_maintenance_pipeline.step_broken_link_scan",
                fromlist=["run"],
            ).run(ctx)
        ))
        _run_step("junk_name_scan", lambda: (
            __import__(
                "app.assistant.pipelines.entity_card_maintenance_pipeline.step_junk_name_scan",
                fromlist=["run"],
            ).run(ctx)
        ))
        _run_step("blank_content_scan", lambda: (
            __import__(
                "app.assistant.pipelines.entity_card_maintenance_pipeline.step_blank_content_scan",
                fromlist=["run"],
            ).run(ctx)
        ))
        _run_step("low_confidence_scan", lambda: (
            __import__(
                "app.assistant.pipelines.entity_card_maintenance_pipeline.step_low_confidence_scan",
                fromlist=["run"],
            ).run(ctx)
        ))
        _run_step("stale_content_scan", lambda: (
            __import__(
                "app.assistant.pipelines.entity_card_maintenance_pipeline.step_stale_content_scan",
                fromlist=["run"],
            ).run(ctx)
        ))
        _run_step("no_link_scan", lambda: (
            __import__(
                "app.assistant.pipelines.entity_card_maintenance_pipeline.step_no_link_scan",
                fromlist=["run"],
            ).run(ctx)
        ))

        logger.debug("[EntityCardMaintenancePipeline] Completed run_id=%s", ctx.run_id)
        return summary
