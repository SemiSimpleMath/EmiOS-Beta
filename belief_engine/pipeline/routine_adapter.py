from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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

    Loops over every domain marked enabled=true in configs/belief_domains.yaml
    and runs BeliefEnginePipeline once per domain.

    A single domain failure does not abort the remaining domains. Each result is
    collected, and the adapter raises once at the end if any domain failed so
    routine_manager records the overall run as failed.

    On full success, the adapter exports beliefs inline so the exported JSON
    stays synchronized with the DB and does not race against a slow upstream run.
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

        results: List[Dict[str, Any]] = []
        successes = 0
        failed_domains: List[str] = []

        for cfg in domains:
            domain_id = cfg.id
            domain_run_id = f"{parent_run_id}:{domain_id}"

            try:
                logger.info(
                    "[BeliefEngineAdapter:%s] starting domain=%s",
                    parent_run_id,
                    domain_id,
                )

                pipeline = BeliefEnginePipeline(
                    domain=domain_id,
                    lookback_days=self.lookback_days,
                )

                result = pipeline.run(run_id=domain_run_id)
                result_status = result.get("status")

                if result_status != "success":
                    logger.error(
                        "[BeliefEngineAdapter:%s] domain=%s returned status=%s",
                        parent_run_id,
                        domain_id,
                        result_status,
                    )
                    results.append(
                        {
                            "domain": domain_id,
                            "status": "error",
                            "result": result,
                        }
                    )
                    failed_domains.append(domain_id)
                    continue

                results.append(
                    {
                        "domain": domain_id,
                        "status": "success",
                        "result": result,
                    }
                )
                successes += 1

                logger.info(
                    "[BeliefEngineAdapter:%s] domain=%s done",
                    parent_run_id,
                    domain_id,
                )

            except Exception as exc:
                logger.exception(
                    "[BeliefEngineAdapter:%s] domain=%s failed: %s",
                    parent_run_id,
                    domain_id,
                    exc,
                )
                results.append(
                    {
                        "domain": domain_id,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                failed_domains.append(domain_id)

        failures = len(failed_domains)

        if failed_domains:
            raise RuntimeError(
                f"belief_engine: {failures}/{len(domains)} domains failed: "
                f"{', '.join(failed_domains)} (successes={successes})"
            )

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
            "results": results,
            "export_path": str(out_path),
        }