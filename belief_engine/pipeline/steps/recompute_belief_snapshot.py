"""
Step: recompute evidence-weighted belief snapshots.

Replaces the old time-threshold DecayStaleBeliefsStep. For each active belief
in the run's domain, aggregate decayed support + contradiction weights from
belief_evidence, derive a confidence band, and write back snapshot columns.

Beliefs that fade out (net weight below floor) get deprecated terminally.
Beliefs flagged contested or contradiction-dominated get status='contested'
so the existing ReevaluateBeliefsStep picks them up next.

Pure-Python aggregation. No LLM calls. Idempotent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from belief_engine.decay.recompute import recompute_belief_snapshots

logger = logging.getLogger(__name__)


class RecomputeBeliefSnapshotStep:
    """Runs after UpdateBeliefsStep (which bumps last_confirmed) and before
    ReevaluateBeliefsStep (which handles the contested keys this step emits).

    Domain-scoped: only touches beliefs in the pipeline's domain. The decay
    math is governed by per-belief `kind` (durable_fact = no decay,
    transient_state = 1-day half-life, etc.). No per-domain on/off flag —
    durable_fact protection makes the decay safe to apply universally.
    """

    name = "recompute_belief_snapshot"

    def __init__(
        self,
        domain: str,
        *,
        now_utc: Optional[datetime] = None,
    ) -> None:
        self.domain = domain
        if now_utc is not None and now_utc.tzinfo is None:
            raise ValueError("now_utc must be timezone-aware")
        self._fixed_now_utc = now_utc

    def inputs(self, ctx: Any) -> list:
        return ["db: user_beliefs", "db: belief_evidence"]

    def outputs(self, ctx: Any) -> list:
        return ["ctx.snapshot_result"]

    def run(self, ctx: Any) -> dict:
        now_utc = self._fixed_now_utc or datetime.now(timezone.utc)
        stats = recompute_belief_snapshots(self.domain, now_utc=now_utc)

        # Hand newly-contested keys to ReevaluateBeliefsStep by appending to
        # ctx.belief_update_result["contested_keys"] (same wiring the old
        # decay step used so the downstream step doesn't need to change).
        if stats.contested_flipped > 0:
            # Pull the just-flipped keys for downstream re-eval.
            from sqlalchemy import text
            from app.models.base import get_session
            session = get_session()
            try:
                rows = session.execute(
                    text(
                        "SELECT belief_key FROM user_beliefs "
                        "WHERE domain = :d AND status = 'contested' "
                        "AND current_confidence_band IN ('contested', 'deprecated_by_contradiction')"
                    ),
                    {"d": self.domain},
                ).fetchall()
                contested_keys = [r[0] for r in rows]
            finally:
                session.close()
            if contested_keys:
                update_result = getattr(ctx, "belief_update_result", None) or {}
                existing = list(update_result.get("contested_keys") or [])
                merged = existing + [k for k in contested_keys if k not in existing]
                update_result["contested_keys"] = merged
                ctx.belief_update_result = update_result

        result = {
            "status": "success",
            "domain": self.domain,
            "now_utc": now_utc.isoformat(),
            "scanned": stats.scanned,
            "updated": stats.updated,
            "faded": stats.faded,
            "contested_flipped": stats.contested_flipped,
            "skipped_no_evidence": stats.skipped_no_evidence,
            "errors": stats.errors,
        }
        ctx.snapshot_result = result
        logger.info(
            "[RecomputeBeliefSnapshot] domain=%s scanned=%d updated=%d faded=%d contested=%d",
            self.domain, stats.scanned, stats.updated, stats.faded, stats.contested_flipped,
        )
        return result
