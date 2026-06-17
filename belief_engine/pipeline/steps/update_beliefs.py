"""
Step 2: Update beliefs based on collected evidence.

For each batch of evidence:
  1. Embed all evidence items and find semantically similar existing beliefs.
  2. Send evidence + existing beliefs to the belief_updater LLM agent.
  3. Apply the agent's decisions (create / update / deprecate / no_change) via BeliefStore.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

from belief_engine.pipeline.steps.collect_evidence import EvidenceBundle, EvidenceItem
from belief_engine.store.belief_store import BeliefStore, BeliefUpsertRequest, EvidenceInput

logger = logging.getLogger(__name__)

_AGENT_NAME = "belief_engine::belief_updater"

# How many existing beliefs to surface per semantic search.
_SIMILAR_K = 8
_SIMILARITY_THRESHOLD = 0.50


def _format_existing_beliefs(beliefs) -> str:
    if not beliefs:
        return "(none)"
    lines = []
    for belief, score in beliefs:
        lines.append(
            f"- key={belief.belief_key!r} confidence={belief.confidence} "
            f"obs={belief.observation_count} similarity={score:.2f}"
        )
        lines.append(f"  Statement: {belief.statement}")
    return "\n".join(lines)


def _to_evidence_input(item: EvidenceItem) -> EvidenceInput:
    return EvidenceInput(
        source_type=item.source_type,
        source_date=item.source_date,
        source_ref=item.source_ref,
        signal_type=item.signal_type,
        summary=item.summary,
        raw_text=item.raw_text,
        weight=item.weight,
        extracted_by="belief_engine::belief_updater",
    )


def _resolve_evidence_by_refs(
    items: List[EvidenceItem], refs: List[int]
) -> List[EvidenceItem]:
    """Resolve 1-based evidence indices (as emitted by the LLM) to EvidenceItem
    objects. Out-of-range indices are logged and skipped. Empty refs → attach
    no evidence (the LLM chose not to cite any)."""
    if not refs:
        return []
    resolved = []
    for ref in refs:
        idx = ref - 1  # convert to 0-based
        if 0 <= idx < len(items):
            resolved.append(items[idx])
        else:
            logger.debug("[UpdateBeliefsStep] evidence_ref %d out of range (batch size=%d)", ref, len(items))
    return resolved


class UpdateBeliefsStep:
    name = "update_beliefs"

    def __init__(self, domain: str) -> None:
        # Validate domain config up front so a typo doesn't silently produce
        # an empty existing-beliefs block and zero matches downstream.
        from belief_engine.config import get_domain_config
        if get_domain_config(domain) is None:
            raise ValueError(
                f"Unknown belief-engine domain {domain!r} — no entry in domain config."
            )
        self.domain = domain

    def inputs(self, ctx: Any) -> list:
        return ["evidence_bundle from collect_evidence", "db: user_beliefs"]

    def outputs(self, ctx: Any) -> list:
        return []

    def run(self, ctx: Any) -> dict:
        bundle: Optional[EvidenceBundle] = getattr(ctx, "evidence_bundle", None)
        if bundle is None or bundle.is_empty():
            logger.info("[UpdateBeliefsStep] no evidence for domain=%s — skipping", self.domain)
            ctx.belief_update_result = {"status": "skipped", "reason": "no_evidence"}
            return ctx.belief_update_result

        store = BeliefStore()

        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")
        agent = agent_factory.create_agent(_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"Agent {_AGENT_NAME!r} not found")

        items = bundle.items
        evidence_block = bundle.as_block()

        # One semantic query over the whole evidence batch — coarse but cheap;
        # surfaces beliefs related to the overall topic mix rather than each
        # individual item. Adequate for the small batches we run today.
        combined_query = " ".join(item.summary for item in items)
        existing_hits = store.find_similar(
            combined_query,
            k=_SIMILAR_K,
            domain=self.domain,
            threshold=_SIMILARITY_THRESHOLD,
        )
        existing_block = _format_existing_beliefs(existing_hits)

        # Scope built once by the pipeline, threaded via ctx — this LLM step
        # receives it, does not build its own.
        msg = Message(
            agent_input={
                "task": f"Process new evidence for the '{self.domain}' domain.",
                "evidence_block": evidence_block,
                "existing_beliefs_block": existing_block,
                "date_today": get_local_time_str(),
                "domain": self.domain,
            },
            scope_context=ctx.scope_context,
        )

        resp = agent.action_handler(msg)
        payload = resp.data if resp and hasattr(resp, "data") else {}
        belief_outputs = payload.get("beliefs") or []

        stats = {"created": 0, "updated": 0, "deprecated": 0, "no_change": 0, "errors": 0, "contested": 0}
        # Beliefs that need re-evaluation (confidence dropped or explicitly contested)
        contested_keys: List[str] = []
        today_iso = datetime.now(timezone.utc).date().isoformat()

        _CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

        for bo in belief_outputs:
            try:
                action = bo.get("action", "no_change")
                belief_key = bo.get("belief_key", "")
                if not belief_key:
                    logger.warning("[UpdateBeliefsStep] skipping belief with empty key")
                    continue

                # Honor a manual lock: a belief the owner corrected + locked via /beliefs must
                # not be re-evolved or deprecated. Downgrade a mutating action to no_change so
                # evidence still attaches and observation_count/last_confirmed advance, but the
                # statement and status stay exactly as the owner set them.
                if action in ("update", "deprecate"):
                    locked_row = store.get_by_key(belief_key)
                    if locked_row and getattr(locked_row, "locked", 0):
                        logger.info(
                            "[UpdateBeliefsStep] %s is LOCKED — preserving manual correction (was action=%s)",
                            belief_key, action,
                        )
                        action = "no_change"

                if action == "deprecate":
                    store.deprecate(belief_key, reason=bo.get("reasoning", ""))
                    stats["deprecated"] += 1
                    continue

                if action == "no_change":
                    # Still upsert to increment observation_count and attach evidence.
                    existing = store.get_by_key(belief_key)
                    if existing:
                        req = BeliefUpsertRequest(
                            domain=self.domain,
                            belief_key=belief_key,
                            statement=existing.statement,
                            confidence=existing.confidence,
                            scope=existing.scope,
                            status=existing.status,
                            last_confirmed=today_iso,
                        )
                        refs = bo.get("evidence_refs") or []
                        relevant_ev = _resolve_evidence_by_refs(items, refs)
                        store.upsert_belief(req, [_to_evidence_input(ev) for ev in relevant_ev])
                        stats["no_change"] += 1
                    continue

                new_confidence = bo.get("confidence", "medium")
                new_status = bo.get("status", "active")
                new_statement = (bo.get("statement") or "").strip()

                # Refuse to upsert an empty statement — would blank the belief
                # in the store. The agent_form requires statement; this guards
                # against degenerate output.
                if not new_statement:
                    logger.warning(
                        "[UpdateBeliefsStep] empty statement on %s action=%s — skipping",
                        belief_key, action,
                    )
                    continue

                # Fetch existing before upsert to detect confidence drops
                existing_before = store.get_by_key(belief_key) if action == "update" else None
                prev_confidence = existing_before.confidence if existing_before else new_confidence

                req = BeliefUpsertRequest(
                    domain=self.domain,
                    belief_key=belief_key,
                    statement=new_statement,
                    confidence=new_confidence,
                    scope=bo.get("scope", "chronic"),
                    status=new_status,
                    last_confirmed=today_iso,
                    kind=bo.get("kind"),
                )
                refs = bo.get("evidence_refs") or []
                relevant_ev = _resolve_evidence_by_refs(items, refs)
                evidence_inputs = [_to_evidence_input(ev) for ev in relevant_ev]
                store.upsert_belief(req, evidence_inputs)
                logger.debug(
                    "[UpdateBeliefsStep] attached %d/%d evidence items to %s (refs=%s)",
                    len(relevant_ev), len(items), belief_key, refs,
                )

                # Flag for re-evaluation if:
                # 1. Status was set to contested (applies to BOTH create and update —
                #    a fresh-but-contested belief needs the reevaluator too), OR
                # 2. Confidence dropped from a known previous value (update only).
                if new_status == "contested":
                    contested_keys.append(belief_key)
                    store.mark_contested(belief_key)
                    stats["contested"] += 1
                    logger.info("[UpdateBeliefsStep] marked contested: %s", belief_key)
                elif action == "update" and _CONFIDENCE_RANK.get(new_confidence, 1) < _CONFIDENCE_RANK.get(prev_confidence, 1):
                    contested_keys.append(belief_key)
                    store.mark_contested(belief_key)
                    stats["contested"] += 1
                    logger.info(
                        "[UpdateBeliefsStep] confidence drop %s→%s on %s → queued for re-eval",
                        prev_confidence, new_confidence, belief_key,
                    )

                if action == "create":
                    stats["created"] += 1
                else:
                    stats["updated"] += 1

                logger.info(
                    "[UpdateBeliefsStep] %s belief key=%s confidence=%s",
                    action, belief_key, req.confidence,
                )
            except Exception as exc:
                stats["errors"] += 1
                logger.exception(
                    "[UpdateBeliefsStep] failed processing belief key=%s: %s",
                    bo.get("belief_key", "?"), exc,
                )

        logger.info("[UpdateBeliefsStep] domain=%s stats=%s", self.domain, stats)
        ctx.belief_update_result = {
            "status": "partial_error" if stats["errors"] > 0 else "ok",
            "domain": self.domain,
            "stats": stats,
            "contested_keys": contested_keys,
        }
        return ctx.belief_update_result
