from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BeliefEngineExportAdapter:
    """
    Runs a full export of all active beliefs to resource_user_beliefs.json.
    Registered as a separate pipeline so it runs once daily after the
    unified domain pipeline has completed. Export is content-guarded —
    no write if nothing changed.
    """

    pipeline_id = "belief_engine_export"

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        only_steps: Optional[list] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict:
        from belief_engine.export.export_beliefs import export_beliefs

        # Let exceptions bubble — routine_manager wraps the dispatch in
        # try/except and records status=error only on a raised exception,
        # not on a returned error dict.
        out_path = export_beliefs()
        logger.info("[BeliefEngineExportAdapter] export complete → %s", out_path)
        return {"pipeline_id": self.pipeline_id, "status": "success", "path": str(out_path)}


class BeliefEngineAdapter:
    """
    Unified belief-engine pipeline adapter.

    Loops over every domain marked ``enabled: true`` in
    ``configs/belief_domains.yaml`` and runs the per-domain
    BeliefEnginePipeline in sequence. Replaces the formerly per-domain
    BeliefEngineRoutineAdapter (one registration per domain) — adding /
    disabling a domain is now a YAML edit rather than a code + routines.json
    edit.

    A single domain failure is logged but does not abort the rest of the
    loop — the adapter collects per-domain results then raises once at
    the end if any domain failed, so routine_manager records the run as
    failed with a summary of which domains broke.

    On success, the adapter calls export_beliefs() at the end so the
    exported JSON stays in lock-step with the DB. This replaces the
    previously-separate fixed-time belief_engine_export routine, which
    raced if belief_engine ran long.
    """

    pipeline_id = "belief_engine"

    def __init__(self, lookback_days: int = 14) -> None:
        self.lookback_days = lookback_days

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        only_steps: Optional[list] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        from belief_engine.config import list_enabled_domains
        from belief_engine.pipeline.pipeline import BeliefEnginePipeline

        domains = list_enabled_domains()
        if not domains:
            logger.warning("[BeliefEngineAdapter] no enabled domains in configs/belief_domains.yaml")
            return {"pipeline_id": self.pipeline_id, "status": "no_domains", "results": []}

        results: List[Dict[str, Any]] = []
        successes = 0
        failed_domains: List[str] = []
        for cfg in domains:
            try:
                pipeline = BeliefEnginePipeline(
                    domain=cfg.id, lookback_days=self.lookback_days,
                )
                result = pipeline.run(run_id=run_id)
                results.append({"domain": cfg.id, "status": "success", "result": result})
                successes += 1
                logger.info("[BeliefEngineAdapter] domain=%s done", cfg.id)
            except Exception as exc:
                # Per the class docstring, a single domain failure does not
                # abort the rest of the loop. We record it and continue, then
                # raise once at the end so routine_manager records the run
                # as failed.
                logger.exception("[BeliefEngineAdapter] domain=%s failed: %s", cfg.id, exc)
                results.append({"domain": cfg.id, "status": "error", "error": str(exc)})
                failed_domains.append(cfg.id)

        if failed_domains:
            raise RuntimeError(
                f"belief_engine: {len(failed_domains)}/{len(domains)} domains failed: "
                f"{', '.join(failed_domains)} (successes={successes})"
            )

        # Export inline so the JSON cannot diverge from the DB. If export
        # raises, the routine fails loudly — better than scheduling export
        # at a fixed time and risking a race against a slow upstream run.
        from belief_engine.export.export_beliefs import export_beliefs
        out_path = export_beliefs()
        logger.info("[BeliefEngineAdapter] export complete → %s", out_path)

        return {
            "pipeline_id": self.pipeline_id,
            "status": "success",
            "successes": successes,
            "failures": 0,
            "results": results,
            "export_path": str(out_path),
        }
