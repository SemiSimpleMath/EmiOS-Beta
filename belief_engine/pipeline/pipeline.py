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

from belief_engine.config import get_domain_config
from belief_engine.pipeline.steps.collect_evidence import CollectEvidenceStep
from belief_engine.pipeline.steps.update_beliefs import UpdateBeliefsStep
from belief_engine.pipeline.steps.decay_stale_beliefs import DecayStaleBeliefsStep
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


class BeliefEnginePipeline:
    pipeline_id = "belief_engine"

    def __init__(self, domain: str = "routine", lookback_days: int = 14) -> None:
        self.domain = domain
        self.lookback_days = lookback_days

    def run(self, *, run_id: Optional[str] = None) -> Dict:
        run_id = run_id or uuid.uuid4().hex[:8]
        ctx = _RunContext(domain=self.domain, run_id=run_id)
        started = datetime.now(timezone.utc).isoformat()
        step_results = []

        # DecayStaleBeliefsStep is opt-in per domain via configs/belief_domains.yaml.
        # Insert between Update (which bumps last_confirmed for touched beliefs)
        # and Reevaluate (which handles contested keys, including the chronics
        # flagged by the decay step).
        domain_cfg = get_domain_config(self.domain)
        decay_on = bool(domain_cfg and domain_cfg.decay_enabled)

        steps = [
            CollectEvidenceStep(self.domain, self.lookback_days),
            UpdateBeliefsStep(self.domain),
        ]
        if decay_on:
            steps.append(DecayStaleBeliefsStep(self.domain))
            logger.info("[BeliefEnginePipeline:%s] decay_enabled=true for domain=%s", run_id, self.domain)
        steps.extend([
            ReevaluateBeliefsStep(),
            CanonicalizeBeliefSetStep(self.domain),
        ])

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
