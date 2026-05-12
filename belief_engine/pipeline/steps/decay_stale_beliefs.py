"""
Step: time-based decay.

Honors the `scope` field that the belief_updater agent already sets on every
belief — `temporary` ("will decay") vs `chronic` (durable):

  - active+temporary unconfirmed for >30 days  → deprecated
                                                 (auto-prune; agent's intent)
  - active+chronic   unconfirmed for >180 days → contested
                                                 (handed off to ReevaluateBeliefsStep
                                                  for an LLM review pass)

Deterministic. No LLM calls of its own. Reuses BeliefStore.deprecate() for the
temporary path so each deprecation gets an audit evidence row, and
BeliefStore.flag_stale_chronic_beliefs() for the chronic path.

Run after UpdateBeliefsStep so any belief touched by today's evidence has its
last_confirmed already bumped — fresh confirmations correctly exclude a belief
from decay this tick.

Output: appends contested chronic keys onto ctx.belief_update_result["contested_keys"]
so ReevaluateBeliefsStep picks them up.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from belief_engine.store.belief_store import BeliefStore

logger = logging.getLogger(__name__)

DEFAULT_TEMPORARY_TTL_DAYS = 30
DEFAULT_CHRONIC_REVIEW_TTL_DAYS = 180


class DecayStaleBeliefsStep:
    """
    Run after UpdateBeliefsStep, before ReevaluateBeliefsStep.

    Domain-scoped: only touches beliefs in this pipeline's domain.
    """

    name = "decay_stale_beliefs"

    def __init__(
        self,
        domain: str,
        *,
        temporary_ttl_days: int = DEFAULT_TEMPORARY_TTL_DAYS,
        chronic_review_ttl_days: int = DEFAULT_CHRONIC_REVIEW_TTL_DAYS,
        now_utc: Optional[datetime] = None,
    ) -> None:
        if temporary_ttl_days <= 0:
            raise ValueError(f"temporary_ttl_days must be positive (got {temporary_ttl_days})")
        if chronic_review_ttl_days <= 0:
            raise ValueError(f"chronic_review_ttl_days must be positive (got {chronic_review_ttl_days})")
        if now_utc is not None:
            if now_utc.tzinfo is None:
                raise ValueError("now_utc must be timezone-aware")
            now_utc = now_utc.astimezone(timezone.utc)

        self.domain = domain
        self.temporary_ttl_days = temporary_ttl_days
        self.chronic_review_ttl_days = chronic_review_ttl_days
        # Injectable for the sandbox / tests; production callers leave it None
        # and we resolve to real now at run() time.
        self._fixed_now_utc = now_utc

    def inputs(self, ctx: Any) -> list:
        return ["db: user_beliefs", "db: belief_evidence"]

    def outputs(self, ctx: Any) -> list:
        return ["ctx.belief_update_result.contested_keys (appended)"]

    def run(self, ctx: Any) -> dict:
        now_utc = self._fixed_now_utc or datetime.now(timezone.utc)
        store = BeliefStore()

        deprecated = store.decay_temporary_beliefs(
            self.domain,
            now_utc=now_utc,
            threshold_days=self.temporary_ttl_days,
        )
        contested = store.flag_stale_chronic_beliefs(
            self.domain,
            now_utc=now_utc,
            threshold_days=self.chronic_review_ttl_days,
        )

        # Hand newly-contested chronics to the existing ReevaluateBeliefsStep
        # by appending into update_result.contested_keys. Make a defensive copy
        # so we don't double-process if update already flagged the same key.
        if contested:
            update_result = getattr(ctx, "belief_update_result", None) or {}
            existing = list(update_result.get("contested_keys") or [])
            merged = existing + [k for k in contested if k not in existing]
            update_result["contested_keys"] = merged
            ctx.belief_update_result = update_result

        result = {
            "status": "success",
            "domain": self.domain,
            "now_utc": now_utc.isoformat(),
            "temporary_deprecated": deprecated,
            "chronic_contested": contested,
            "temporary_ttl_days": self.temporary_ttl_days,
            "chronic_review_ttl_days": self.chronic_review_ttl_days,
        }
        ctx.decay_result = result
        logger.info(
            "[DecayStaleBeliefs] domain=%s deprecated=%d contested=%d",
            self.domain, len(deprecated), len(contested),
        )
        return result
