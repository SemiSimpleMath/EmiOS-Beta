from __future__ import annotations

from app.assistant.utils.logging_config import get_logger
import uuid
from typing import Any, Dict, List, Optional

logger = get_logger(__name__)


class BeliefEngineExportAdapter:
    """
    Manual/export-only adapter for belief-engine exports.

    The main BeliefEngineAdapter now exports inline after all enabled domains
    complete successfully, so this adapter should only be used for manual
    export runs, debugging, or backfills.

    Export is content-guarded by export_beliefs(), so no write occurs if the
    exported belief content has not changed.
    """

    pipeline_id = "belief_engine_export"

    def run(
            self,
            *,
            target_date: Optional[str] = None,
            only_steps: Optional[List[str]] = None,
            run_id: Optional[str] = None,
            force: bool = False,
    ) -> Dict[str, Any]:
        """
        Run a standalone belief export.

        The extra arguments are accepted for routine-manager compatibility.
        """
        _ = target_date, only_steps, run_id, force

        from belief_engine.export.export_beliefs import export_beliefs

        out_path = export_beliefs()
        logger.info("[BeliefEngineExportAdapter] export complete -> %s", out_path)

        return {
            "pipeline_id": self.pipeline_id,
            "status": "success",
            "path": str(out_path),
        }


class BeliefEngineAdapter:
    """
    Unified belief-engine pipeline adapter.

    Runs BeliefEnginePipeline ONCE, globally: the evidence of every domain marked
    enabled=true in configs/belief_domains.yaml goes into one bundle, the updater files
    each belief under its primary area, and dedup sees the whole active set. A failing
    step raises so routine_manager records the run as failed.

    On success, the adapter exports beliefs inline so the exported JSON stays
    synchronized with the DB and does not race against a slow upstream run.
    """

    pipeline_id = "belief_engine"

    def __init__(self, lookback_days: int = 14) -> None:
        self.lookback_days = lookback_days

    def run(
            self,
            *,
            target_date: Optional[str] = None,
            only_steps: Optional[List[str]] = None,
            run_id: Optional[str] = None,
            force: bool = False,
    ) -> Dict[str, Any]:
        """
        Run the belief engine for all enabled domains.

        The extra arguments are accepted for routine-manager compatibility.
        """
        _ = target_date, only_steps, force

        from belief_engine.config import list_enabled_domains
        from belief_engine.export.export_beliefs import export_beliefs
        from belief_engine.pipeline.pipeline import BeliefEnginePipeline

        parent_run_id = run_id or uuid.uuid4().hex[:12]

        domains = list_enabled_domains()
        if not domains:
            logger.warning(
                "[BeliefEngineAdapter:%s] no enabled domains in configs/belief_domains.yaml",
                parent_run_id,
            )
            return {
                "pipeline_id": self.pipeline_id,
                "run_id": parent_run_id,
                "status": "no_domains",
                "successes": 0,
                "failures": 0,
                "results": [],
            }

        # ONE global pass: every enabled domain's evidence in one bundle, bounded updater batches
        # that file each belief under its primary area, and dedup that can see across
        # areas. The per-domain loop this replaced minted one copy of the same belief per
        # domain that matched an insight's tags (the "Panda Express x4" bloat).
        logger.info(
            "[BeliefEngineAdapter:%s] starting global pass over %d domains: %s",
            parent_run_id, len(domains), ", ".join(cfg.id for cfg in domains),
        )
        pipeline = BeliefEnginePipeline(domain=None, lookback_days=self.lookback_days)
        result = pipeline.run(run_id=parent_run_id)
        results: List[Dict[str, Any]] = [{"domain": "global", "status": result.get("status"), "result": result}]

        if result.get("status") != "success":
            failed_step = next((s for s in result.get("steps", []) if s.get("status") == "error"), {})
            raise RuntimeError(
                f"belief_engine: global pass failed at step {failed_step.get('step', '?')!r}: "
                f"{failed_step.get('error', '')}"
            )
        successes, failures = 1, 0

        # A tolerated per-item failure does not fail the run, so it would otherwise leave no
        # trace above the pipeline. Repeat it at ERROR here and carry it in the run record.
        tolerated = result.get("tolerated_failures") or []
        if tolerated:
            logger.error(
                "[BeliefEngineAdapter:%s] global pass SUCCEEDED WITH TOLERATED FAILURES — these items "
                "were skipped and not written, and will be retried on the next run: %s",
                parent_run_id, tolerated,
            )

        # A one-off slip heals on the next run, so only a belief failing REPEATEDLY is worth
        # interrupting him for. Skipped when the update step did not run at all, so an
        # evidence-free night cannot clear a real streak.
        update = result.get("belief_update") or {}
        persistent: List[Dict[str, Any]] = []
        if update.get("status") != "skipped":
            from belief_engine.db.paths import belief_db_path
            from belief_engine.pipeline import tolerated_failures
            failed_beliefs = [f for t in tolerated for f in (t.get("failed_items") or [])
                              if isinstance(f, dict)]
            persistent = tolerated_failures.record_run(belief_db_path(), failed_beliefs)
            if persistent:
                logger.error(
                    "[BeliefEngineAdapter:%s] %d belief(s) have now failed %d+ consecutive runs "
                    "and are not being recorded: %s",
                    parent_run_id, len(persistent), tolerated_failures.THRESHOLD,
                    [row["belief_key"] for row in persistent],
                )
                tolerated_failures.surface_ticket(persistent)

        out_path = export_beliefs()
        logger.info(
            "[BeliefEngineAdapter:%s] export complete -> %s",
            parent_run_id,
            out_path,
        )

        return {
            "pipeline_id": self.pipeline_id,
            "run_id": parent_run_id,
            "status": "success",
            "successes": successes,
            "failures": failures,
            "tolerated_failures": tolerated,
            "persistent_failures": persistent,
            "results": results,
            "export_path": str(out_path),
        }