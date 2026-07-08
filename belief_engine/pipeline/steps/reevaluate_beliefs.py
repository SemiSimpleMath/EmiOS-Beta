"""
Step 3 (conditional): Re-evaluate beliefs that are contested or have dropped confidence.

Only runs when update_beliefs produced contested_keys. For each contested belief:
  1. Load the full evidence trail from DB.
  2. Send to belief_reevaluator agent with the complete history.
  3. Apply the authoritative rewrite — can change statement, add qualifiers, split, or deprecate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

from belief_engine.store.belief_store import BeliefStore, BeliefRecord, BeliefUpsertRequest

logger = logging.getLogger(__name__)

_AGENT_NAME = "belief_engine::belief_reevaluator"
# Max evidence items to send — most beliefs won't have more than this
_MAX_EVIDENCE = 50


def _format_belief_block(belief: BeliefRecord) -> str:
    return (
        f"key: {belief.belief_key}\n"
        f"statement: {belief.statement}\n"
        f"confidence: {belief.confidence}  scope: {belief.scope}  status: {belief.status}\n"
        f"observation_count: {belief.observation_count}\n"
        f"first_observed: {belief.first_observed}  last_confirmed: {belief.last_confirmed}"
    )


def _format_evidence_trail(store: BeliefStore, belief: BeliefRecord) -> str:
    evidence = store.get_evidence(belief.id)
    if not evidence:
        return "(no stored evidence)"
    # Sort oldest first, cap at _MAX_EVIDENCE
    evidence = sorted(evidence, key=lambda e: e.source_date or e.created_at)[-_MAX_EVIDENCE:]
    lines = []
    for ev in evidence:
        date_tag = ev.source_date or ev.created_at[:10]
        weight_tag = f"weight={ev.weight:.1f}"
        lines.append(
            f"[{date_tag}][{ev.signal_type}][{ev.source_type}] {weight_tag}\n"
            f"  {ev.summary}"
            + (f"\n  user_comment: \"{ev.raw_text[:200]}\"" if ev.raw_text else "")
        )
    return "\n\n".join(lines)


class ReevaluateBeliefsStep:
    """
    Runs only when there are contested beliefs from UpdateBeliefsStep.
    Sends the full evidence trail to belief_reevaluator for authoritative rewrite.
    """

    name = "reevaluate_beliefs"

    def inputs(self, ctx: Any) -> list:
        return ["contested_keys from update_beliefs", "db: belief_evidence"]

    def outputs(self, ctx: Any) -> list:
        return []

    def run(self, ctx: Any) -> dict:
        update_result = getattr(ctx, "belief_update_result", None) or {}
        contested_keys: List[str] = update_result.get("contested_keys") or []

        if not contested_keys:
            logger.info("[ReevaluateBeliefs] no contested beliefs — skipping")
            ctx.reevaluation_result = {"status": "skipped", "reason": "no_contested_beliefs"}
            return ctx.reevaluation_result

        store = BeliefStore()
        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")

        stats = {"rewritten": 0, "qualified": 0, "split": 0, "deprecated": 0, "confirmed": 0,
                 "locked_skipped": 0, "errors": 0}
        notes_collected: List[str] = []
        today_iso = datetime.now(timezone.utc).date().isoformat()

        for belief_key in contested_keys:
            belief = store.get_by_key(belief_key)
            if belief is None:
                logger.warning("[ReevaluateBeliefs] belief not found: %s", belief_key)
                continue

            # Honor a manual lock: a belief the owner corrected + locked via /beliefs keeps its
            # statement and status exactly as the owner set them — no rewrite, split, or
            # deprecation here, and no LLM spent on it. (/beliefs normalizes contested->active
            # at lock time, so a locked belief reaching this list is a legacy edge.)
            if getattr(belief, "locked", 0):
                stats["locked_skipped"] += 1
                logger.info("[ReevaluateBeliefs] %s is LOCKED — preserving the owner's correction",
                            belief_key)
                continue

            try:
                belief_block = _format_belief_block(belief)
                evidence_trail_block = _format_evidence_trail(store, belief)

                agent = agent_factory.create_agent(_AGENT_NAME)
                if agent is None:
                    raise RuntimeError(f"Agent '{_AGENT_NAME}' not found")

                # Scope built once by the pipeline, threaded via ctx — this LLM
                # step receives it, does not build its own.
                msg = Message(
                    agent_input={
                        "task": f"Re-evaluate belief '{belief_key}' using its complete evidence trail.",
                        "belief_block": belief_block,
                        "evidence_trail_block": evidence_trail_block,
                        "domain": ctx.domain,
                        "date_today": get_local_time_str(),
                    },
                    scope_context=ctx.scope_context,
                )

                resp = agent.action_handler(msg)
                payload = resp.data if resp and hasattr(resp, "data") else {}
                revised_beliefs = payload.get("revised_beliefs") or []
                reevaluation_notes = payload.get("reevaluation_notes")
                if reevaluation_notes:
                    notes_collected.append(f"[{belief_key}] {reevaluation_notes}")

                # Track whether anything in revised_beliefs actually touches the
                # original belief_key. If a split returns only NEW keys (i.e.
                # the agent failed to use the original key for the primary child
                # as instructed), the original would otherwise be orphaned —
                # we deprecate it after the loop as a safety net.
                original_key_touched = False

                for rb in revised_beliefs:
                    action = rb.get("action", "confirm")
                    revised_key = rb.get("belief_key", belief_key)
                    revised_statement = (rb.get("statement") or "").strip()

                    if revised_key == belief.belief_key:
                        original_key_touched = True

                    if action == "deprecate":
                        store.deprecate(revised_key, reason=rb.get("reasoning", "re-evaluation"))
                        stats["deprecated"] += 1
                        logger.info("[ReevaluateBeliefs] deprecated %s", revised_key)
                        continue

                    if action == "confirm":
                        # Confirmed means "still true today" — always upsert so
                        # last_confirmed bumps, regardless of prior status. If
                        # the agent supplied a revised statement, use it;
                        # otherwise keep the existing one.
                        req = BeliefUpsertRequest(
                            domain=ctx.domain,
                            belief_key=revised_key,
                            statement=revised_statement or belief.statement,
                            confidence=rb.get("confidence", belief.confidence),
                            scope=rb.get("scope", belief.scope),
                            status="active",
                            last_confirmed=today_iso,
                        )
                        store.upsert_belief(req)
                        stats["confirmed"] += 1
                        logger.info("[ReevaluateBeliefs] confirmed %s", revised_key)
                        continue

                    # rewrite | qualify | split — statement is authoritative.
                    # Refuse to upsert an empty statement (would blank the belief).
                    if not revised_statement:
                        logger.warning(
                            "[ReevaluateBeliefs] empty statement for action=%s key=%s — skipping",
                            action, revised_key,
                        )
                        continue

                    conditions_val: Optional[dict] = None
                    raw_conditions = rb.get("conditions")
                    if raw_conditions:
                        conditions_val = {"text": raw_conditions}

                    req = BeliefUpsertRequest(
                        domain=ctx.domain,
                        belief_key=revised_key,
                        statement=revised_statement,
                        confidence=rb.get("confidence", belief.confidence),
                        scope=rb.get("scope", belief.scope),
                        status=rb.get("status", "active"),
                        conditions=conditions_val,
                        last_confirmed=today_iso,
                    )
                    store.upsert_belief(req)
                    logger.info(
                        "[ReevaluateBeliefs] %s %s confidence=%s",
                        action, revised_key, req.confidence,
                    )

                    if action == "rewrite":
                        stats["rewritten"] += 1
                    elif action == "qualify":
                        stats["qualified"] += 1
                    elif action == "split":
                        stats["split"] += 1

                # Safety net for split: if the agent emitted revised beliefs
                # but none used the original key, the original is now orphaned.
                # Deprecate it so the belief store doesn't accumulate stale
                # parents alongside narrower children.
                if revised_beliefs and not original_key_touched:
                    store.deprecate(
                        belief.belief_key,
                        reason="superseded by re-evaluation (split into new keys)",
                    )
                    stats["deprecated"] += 1
                    logger.info(
                        "[ReevaluateBeliefs] auto-deprecated orphaned original %s",
                        belief.belief_key,
                    )

            except Exception as exc:
                stats["errors"] += 1
                logger.exception(
                    "[ReevaluateBeliefs] failed re-evaluating belief %s: %s",
                    belief_key, exc,
                )

        logger.info("[ReevaluateBeliefs] domain=%s stats=%s", ctx.domain, stats)
        ctx.reevaluation_result = {
            "status": "partial_error" if stats["errors"] > 0 else "ok",
            "domain": ctx.domain,
            "contested_count": len(contested_keys),
            "stats": stats,
            "notes": notes_collected,
        }
        return ctx.reevaluation_result
