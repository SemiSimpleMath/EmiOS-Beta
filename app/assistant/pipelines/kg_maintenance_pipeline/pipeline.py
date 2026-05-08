"""
KGMaintenancePipeline — periodic graph quality scan.

Steps (run in order, each self-manages its own sessions):
  1. orphan_scan                — zero-edge nodes                    (cheap, structural)
  2. context_embedding_backfill — backfill context embeddings        (embedding API, no LLM)
  3. duplicate_scan             — three-tier candidates + LLM confirm (most expensive)
  4. pagerank                   — weighted PageRank, writes scores   (cheap, no LLM)
  5. description_fill           — LLM descriptions for top-N nodes   (LLM, batched)
  6. missing_dates_scan         — bounded-category States/Events with NULL start_date,
                                  scored by top-2 sum of connected entity pageranks
                                  (cheap, structural; feeds the date-gap question queue)

Execution of findings is NOT automatic — findings land in kg_maintenance_finding
for human review in the /kg-maintenance UI.  The user approves or rejects, then
triggers execution explicitly. (See docs/architecture/13_KG_INVESTIGATION_MUTATION.md
for the alternate LLM-driven path via kg_investigation_manager + kg_mutation_manager.)
"""
from __future__ import annotations

from typing import Optional

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class KGMaintenancePipeline:
    pipeline_id = "kg_maintenance_pipeline"

    def run(
        self,
        *,
        skip_steps: Optional[list[str]] = None,
        description_max_nodes: int = 50,
        target_date: Optional[str] = None,
        run_id: Optional[str] = None,
        only_steps: Optional[list[str]] = None,
        force: bool = False,
    ) -> dict:
        """
        Returns {"run_id": str, "steps": {step_name: step_result | {"error": str}}}.

        Each step runs independently; a failure in one step is recorded in the
        summary and the pipeline continues to the next step.

        skip_steps: list of step names to skip.
        only_steps: if provided, only these steps run (overrides skip_steps).
        description_max_nodes: cap on how many nodes get descriptions per run.
        """
        skip = set(skip_steps or [])
        ctx = PipelineContext.for_date(
            pipeline_id=self.pipeline_id,
            target_date=target_date,
            run_id=run_id,
        )
        logger.info("[KGMaintenancePipeline] Starting run_id=%s", ctx.run_id)
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
                logger.info("[KGMaintenancePipeline] %s: %s", name, result)
            except Exception:
                logger.debug("[KGMaintenancePipeline] %s failed", name, exc_info=True)
                summary["steps"][name] = {"error": "step failed — see logs"}

        _run_step("orphan_scan", lambda: (
            __import__("app.assistant.pipelines.kg_maintenance_pipeline.step_orphan_scan", fromlist=["run"]).run(ctx)
        ))
        _run_step("context_embedding_backfill", lambda: (
            __import__("app.assistant.pipelines.kg_maintenance_pipeline.step_context_embedding_backfill", fromlist=["run"]).run(ctx)
        ))
        _run_step("duplicate_scan", lambda: (
            __import__("app.assistant.pipelines.kg_maintenance_pipeline.step_duplicate_scan", fromlist=["run"]).run(ctx)
        ))
        _run_step("pagerank", lambda: (
            __import__("app.assistant.pipelines.kg_maintenance_pipeline.step_pagerank", fromlist=["run"]).run(ctx)
        ))
        _run_step("description_fill", lambda: (
            __import__("app.assistant.pipelines.kg_maintenance_pipeline.step_description_fill", fromlist=["run"]).run(ctx, max_nodes=description_max_nodes)
        ))
        # state_decay used to fire here as well, but it's owned by the daily
        # `kg_state_decay` routine (02:45 every night) — running it again here
        # on Monday would just close already-closed States. Dropped to avoid
        # the double-fire.
        # Pagerank must run before this so connected-entity scores reflect
        # the most recent graph state. pagerank is step 4 above; this is
        # step 5 now (after state_decay was removed), so ordering still
        # holds as long as 'pagerank' isn't skipped.
        _run_step("missing_dates_scan", lambda: (
            __import__("app.assistant.pipelines.kg_maintenance_pipeline.step_missing_dates_scan", fromlist=["run"]).run(ctx)
        ))

        # Final pass: hand any findings produced by this run (orphan_scan,
        # duplicate_scan, etc.) to kg_investigation_manager so a structured
        # report is ready for human review or downstream mutation.
        _run_step("investigate_findings", lambda: (
            _investigate_findings_for_run(ctx.run_id)
        ))

        logger.info("[KGMaintenancePipeline] Completed run_id=%s steps=%s", ctx.run_id, list(summary["steps"]))
        return summary


def _investigate_findings_for_run(pipeline_run_id: str) -> dict:
    """Investigate findings created during this pipeline run.

    Bounded — caps at 20 investigations per run so a flood of findings can't
    blow up the routine's wall-clock time.
    """
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.assistant.kg_investigator.finding_processor import investigate_findings
    from app.models.db_manager import get_db_manager

    with get_db_manager().read_session() as s:
        rows = (
            s.query(KGMaintenanceFinding.id)
            .filter(KGMaintenanceFinding.pipeline_run_id == pipeline_run_id)
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
        ids = [r[0] for r in rows]
    return investigate_findings(ids, max_to_investigate=20)
