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
        try:
            out_path = export_beliefs()
            logger.info("[BeliefEngineExportAdapter] export complete → %s", out_path)
            return {"pipeline_id": self.pipeline_id, "status": "success", "path": str(out_path)}
        except Exception as exc:
            logger.exception("[BeliefEngineExportAdapter] export failed: %s", exc)
            return {"pipeline_id": self.pipeline_id, "status": "error", "error": str(exc)}


class BeliefEngineAdapter:
    """
    Unified belief-engine pipeline adapter.

    Loops over every domain marked ``enabled: true`` in
    ``configs/belief_domains.yaml`` and runs the per-domain
    BeliefEnginePipeline in sequence. Replaces the formerly per-domain
    BeliefEngineRoutineAdapter (one registration per domain) — adding /
    disabling a domain is now a YAML edit rather than a code + routines.json
    edit.

    A single domain failure is logged but does not abort the rest. The
    overall status is 'success' if every domain succeeded, 'partial' if
    some failed, 'error' only if every domain failed.
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
        failures = 0
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
                logger.exception("[BeliefEngineAdapter] domain=%s failed: %s", cfg.id, exc)
                results.append({"domain": cfg.id, "status": "error", "error": str(exc)})
                failures += 1

        if successes == 0 and failures > 0:
            overall = "error"
        elif failures > 0:
            overall = "partial"
        else:
            overall = "success"

        return {
            "pipeline_id": self.pipeline_id,
            "status": overall,
            "successes": successes,
            "failures": failures,
            "results": results,
        }
