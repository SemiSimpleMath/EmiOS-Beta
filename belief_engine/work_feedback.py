"""Belief back-propagation — work-object outcomes flow into the belief store.

The mirror of `subconscious/concern_feedback.py`. WorkStore commits a pending receipt with each
belief-linked terminal transition; this module delivers it after commit. `belief_engine::work_outcome`
DECIDES what the outcome means, this code APPLIES it through BeliefStore, and the work outcome is
attached as evidence either way — which is also the reverse link, since the evidence row carries
the work_id in `source_ref`.

Why it exists: on 2026-09-25 a belief said "do not treat the request as completed until he
confirms it is scheduled" while the work object it spawned closed itself on a single delivery.
Neither side could see the other. A delivery is not an outcome, and only the belief knows its own
completion condition.

Delivery failures never roll back completed work. A receipt stays pending until every linked
belief has durably received the outcome, and re-delivery is a no-op because an already-attached
work_outcome evidence row is detected before the model is consulted.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT_NAME = "belief_engine::work_outcome"
_ACTIONS = ("no_change", "resolve", "revise")
_VALENCES = ("support", "contradict", "qualify")
# EvidenceInput.signal_type vocabulary, keyed by the valence the model reports.
_SIGNAL = {"support": "confirms", "contradict": "contradicts", "qualify": "qualifies"}


def _already_recorded(belief_id: str, work_id: str) -> bool:
    """Has this work outcome already been attached to this belief?

    The idempotency fence: a redelivered receipt must not append a second evidence row or
    re-deprecate. Cheaper and safer than asking the model again.
    """
    from belief_engine.db.paths import belief_db_path
    conn = sqlite3.connect(str(belief_db_path()))
    try:
        row = conn.execute(
            "SELECT 1 FROM belief_evidence WHERE belief_id=? AND source_type='work_outcome' "
            "AND source_ref=? LIMIT 1", (belief_id, work_id)).fetchone()
        return row is not None
    finally:
        conn.close()


def _scope():
    """This is an entry point, like BeliefEnginePipeline.run — it derives its own scope rather
    than making three dayflow callers thread one through."""
    from app.assistant.scope.loader import load_scope_for_source
    return load_scope_for_source(
        kind="subsystem", source_id="belief_engine", actor_id="belief_engine_work_outcome",
        identity_overrides={"owner_id": "belief_engine", "surface": "pipeline",
                            "scope_id": "scope::belief_engine::work_outcome"})


def _decide(beliefs, context, user_reply):
    """Ask the agent what the outcome means. Returns {belief_key: outcome dict}."""
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    from app.assistant.utils.pydantic_classes import Message
    factory = ServiceLocator.get("agent_factory")
    if factory is None:
        raise RuntimeError("agent_factory not available in DI")
    agent = factory.create_agent(_AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"Agent {_AGENT_NAME!r} not found")
    supplied = [{"belief_key": b.belief_key, "statement": b.statement, "status": b.status,
                 "confidence": b.confidence, "scope": b.scope, "kind": b.kind,
                 "conditions": b.conditions, "last_confirmed": b.last_confirmed}
                for b in beliefs]
    response = agent.action_handler(Message(
        agent_input={"beliefs": supplied,
                     "work_outcome": {**context, "user_reply": user_reply or {}}},
        scope_context=_scope()))
    payload = getattr(response, "data", None) or {}
    outcomes = payload.get("outcomes") or []

    expected = {b.belief_key for b in beliefs}
    seen = {}
    for outcome in outcomes:
        key = str((outcome or {}).get("belief_key") or "").strip()
        if key not in expected:
            raise ValueError(f"work_outcome returned an unknown belief_key: {key!r}")
        if key in seen:
            raise ValueError(f"work_outcome returned {key!r} twice")
        action = str(outcome.get("action") or "").strip()
        valence = str(outcome.get("valence") or "").strip()
        if action not in _ACTIONS:
            raise ValueError(f"work_outcome returned an invalid action {action!r} for {key}")
        if valence not in _VALENCES:
            raise ValueError(f"work_outcome returned an invalid valence {valence!r} for {key}")
        if action == "revise" and not str(outcome.get("statement") or "").strip():
            raise ValueError(f"work_outcome asked to revise {key} without a statement")
        seen[key] = outcome
    missing = sorted(expected - set(seen))
    if missing:
        raise ValueError(f"work_outcome omitted belief(s): {missing}")
    return seen


def _apply(store_b, belief, outcome, *, work_id, context):
    """Write the decision. The model never touches the store; this does."""
    from belief_engine.store.belief_store import BeliefUpsertRequest, EvidenceInput
    action = outcome["action"]
    valence = outcome["valence"]
    reasoning = str(outcome.get("reasoning") or "").strip()

    # Honour an owner lock exactly as update_beliefs does: evidence still attaches so the
    # history is complete, but a belief the owner corrected is never re-worded or retired.
    if action in ("resolve", "revise") and getattr(belief, "locked", 0):
        logger.info("[belief_work_feedback] %s is LOCKED — recording evidence only (was %s)",
                    belief.belief_key, action)
        action = "no_change"

    evidence = EvidenceInput(
        source_type="work_outcome",
        source_date=(str(context.get("completed_at") or "")[:10] or None),
        signal_type=_SIGNAL[valence],
        summary=reasoning or f"Work object {work_id} ended {context.get('terminal', {}).get('status', '')}.",
        source_ref=work_id,
        raw_text=json.dumps(context, default=str)[:4000],
        weight=1.0,
        valence=valence,
        extracted_by=_AGENT_NAME,
    )
    # Attach BEFORE any deprecation: add_evidence_to_existing refuses a deprecated belief, and
    # the evidence row is the reverse link we most want to survive.
    store_b.add_evidence_to_existing(belief.belief_key, [evidence])

    if action == "resolve":
        store_b.deprecate(belief.belief_key,
                          reason=f"work {work_id} satisfied this belief's condition: {reasoning}"[:500])
        return "resolved"
    if action == "revise":
        store_b.upsert_belief(BeliefUpsertRequest(
            domain=belief.domain, belief_key=belief.belief_key,
            statement=str(outcome["statement"]).strip(),
            confidence=str(outcome.get("confidence") or "").strip() or belief.confidence,
            scope=belief.scope, status=belief.status, conditions=belief.conditions,
            kind=belief.kind,
            last_confirmed=datetime.now(timezone.utc).date().isoformat()))
        return "revised"
    return "unchanged"


def _deliver_receipt(store, receipt) -> bool:
    from app.assistant.subconscious.concern_feedback import _last_user_reply
    from belief_engine.store.belief_store import BeliefStore
    from types import SimpleNamespace

    payload = receipt["payload"]
    work_id, context = receipt["work_id"], payload["context"]
    store_b = BeliefStore()

    pending, applied = [], []
    for key in payload["belief_refs"]:
        belief = store_b.get_by_key(key)
        if belief is None:
            # A key can vanish when a merge retires it. The survivor holds the lineage, but this
            # lane does not follow merges yet, so say so loudly rather than wedge the outbox.
            logger.error("[belief_work_feedback] belief %r from work %s no longer resolves — "
                         "outcome not recorded for it", key, work_id)
            continue
        if _already_recorded(belief.id, work_id):
            applied.append((key, "already_applied"))
            continue
        pending.append(belief)

    if pending:
        snapshot = SimpleNamespace(nodes={n["id"]: SimpleNamespace(**n) for n in payload["reply_nodes"]})
        decisions = _decide(pending, context, _last_user_reply(snapshot))
        for belief in pending:
            applied.append((belief.belief_key,
                            _apply(store_b, belief, decisions[belief.belief_key],
                                   work_id=work_id, context=context)))

    store.acknowledge_belief_feedback(receipt["id"])
    changed = [f"{k}:{r}" for k, r in applied if r not in ("already_applied", "unchanged")]
    if changed:
        logger.info("[belief_work_feedback] work %s (%s) updated belief(s): %s",
                    work_id, receipt["outcome"], ", ".join(changed))
    return bool(changed)


def recover_pending_belief_feedback(store, *, work_id=None) -> int:
    """Retry committed receipts, isolating failures so unrelated closures can deliver."""
    delivered = 0
    if not hasattr(store, "pending_belief_feedback"):
        return 0
    for receipt in store.pending_belief_feedback(work_id):
        try:
            _deliver_receipt(store, receipt)
            delivered += 1
        except Exception:
            logger.exception("[belief_work_feedback] receipt %s for %s could not finish",
                             receipt["id"], receipt["work_id"])
            continue
    return delivered


def propagate_work_outcome_to_beliefs(store, work_id: str, outcome: str) -> None:
    """Post-commit delivery. Never raises into the closure path that called it."""
    try:
        recover_pending_belief_feedback(store, work_id=work_id)
    except Exception:
        logger.exception("[belief_work_feedback] propagation failed for %s (%s)", work_id, outcome)
