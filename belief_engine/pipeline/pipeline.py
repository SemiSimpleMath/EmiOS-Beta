"""
BeliefEnginePipeline — runs for one domain per invocation.

Usage:
    pipeline = BeliefEnginePipeline(domain="routine")
    result = pipeline.run()
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from belief_engine.pipeline.steps.collect_evidence import CollectEvidenceStep
from belief_engine.pipeline.steps.update_beliefs import UpdateBeliefsStep
from belief_engine.pipeline.steps.recompute_belief_snapshot import RecomputeBeliefSnapshotStep
from belief_engine.pipeline.steps.reevaluate_beliefs import ReevaluateBeliefsStep
from belief_engine.pipeline.steps.canonicalize_belief_set import CanonicalizeBeliefSetStep

logger = logging.getLogger(__name__)


@dataclass
class _RunContext:
    """Minimal context object passed between steps."""
    domain: str
    run_id: str
    evidence_bundle: Any = None
    belief_update_result: Optional[Dict] = None
    reevaluation_result: Optional[Dict] = None
    canonicalization_result: Optional[Dict] = None
    # Set by the caller (BeliefEngineAdapter) to "full" or "new_only".
    # CanonicalizeBeliefSetStep reads this and falls back to its own
    # sweep_tracker lookup when None.
    canonicalization_mode: Optional[str] = None


class BeliefEnginePipeline:
    pipeline_id = "belief_engine"

    def __init__(
        self,
        domain: str = "routine",
        lookback_days: int = 14,
        canonicalization_mode: Optional[str] = None,
    ) -> None:
        self.domain = domain
        self.lookback_days = lookback_days
        self.canonicalization_mode = canonicalization_mode

    def run(self, *, run_id: Optional[str] = None) -> Dict:
        run_id = run_id or uuid.uuid4().hex[:8]
        ctx = _RunContext(
            domain=self.domain,
            run_id=run_id,
            canonicalization_mode=self.canonicalization_mode,
        )
        started = datetime.now(timezone.utc).isoformat()
        step_results = []

        # RecomputeBeliefSnapshotStep replaced the old DecayStaleBeliefsStep on
        # 2026-05-11. It's the evidence-weighted decay model: per-belief `kind`
        # drives half-life (durable_fact = no decay). Runs universally — no
        # per-domain on/off flag — because durable_fact protection makes it
        # safe to apply everywhere.
        # Insert between Update (which bumps last_confirmed for touched beliefs)
        # and Reevaluate (which handles contested keys this step flips).
        steps: list = [
            CollectEvidenceStep(self.domain, self.lookback_days),
            UpdateBeliefsStep(self.domain),
            RecomputeBeliefSnapshotStep(self.domain),
            ReevaluateBeliefsStep(),
            CanonicalizeBeliefSetStep(self.domain),
        ]

        overall_status = "success"
        for step in steps:
            t0 = time.monotonic()
            try:
                logger.info("[BeliefEnginePipeline:%s] >> %s", run_id, step.name)
                step.run(ctx)
                elapsed = time.monotonic() - t0
                step_results.append({"step": step.name, "status": "success", "duration_s": round(elapsed, 2)})
                logger.info("[BeliefEnginePipeline:%s] << %s OK (%.1fs)", run_id, step.name, elapsed)
            except Exception as exc:
                elapsed = time.monotonic() - t0
                overall_status = "error"
                step_results.append({
                    "step": step.name,
                    "status": "error",
                    "duration_s": round(elapsed, 2),
                    "error": str(exc)[:500],
                })
                logger.exception("[BeliefEnginePipeline:%s] << %s FAILED", run_id, step.name)
                break

        finished = datetime.now(timezone.utc).isoformat()
        return {
            "pipeline_id": self.pipeline_id,
            "run_id": run_id,
            "domain": self.domain,
            "status": overall_status,
            "started_at_utc": started,
            "finished_at_utc": finished,
            "steps": step_results,
            "belief_update": ctx.belief_update_result,
            "reevaluation": ctx.reevaluation_result,
            "canonicalization": ctx.canonicalization_result,
        }
