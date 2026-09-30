"""Belief back-propagation — work-object outcomes flow into the belief store.

The mirror of `subconscious/concern_feedback.py`. WorkStore commits a pending receipt with each
belief-linked terminal transition; this module delivers it after commit. `belief_engine::work_outcome`
DECIDES what the outcome means, this code APPLIES it to the belief catalog (the intake store,
belief_intake_*, since the 2026-09-29 cutover), and the work outcome is attached as evidence either
way — which is also the reverse link, since the evidence row carries `work:<work_id>` in
`source_ref`. A belief_ref that is not a catalog id (a pre-cutover dotted key) resolves to nothing
and is logged, like any vanished belief.

Why it exists: on 2026-09-25 a belief said "do not treat the request as completed until he
confirms it is scheduled" while the work object it spawned closed itself on a single delivery.
Neither side could see the other. A delivery is not an outcome, and only the belief knows its own
completion condition.

Delivery failures never roll back completed work. A receipt stays pending until every linked
belief has durably received the outcome, and re-delivery is a no-op because an already-attached
work_outcome evidence row is detected before the model is consulted.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT_NAME = "belief_engine::work_outcome"
_ACTIONS = ("no_change", "resolve", "revise")
_VALENCES = ("support", "contradict", "qualify")


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
                 "scope": b.scope, "kind": b.kind, "last_confirmed": b.last_confirmed}
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


def _outcome_day(context) -> str:
    """The local day the work ended — evidence days are local, completed_at is UTC."""
    from app.assistant.utils.time_utils import get_local_time, utc_to_local
    completed = context.get("completed_at")
    return (utc_to_local(completed) if completed else get_local_time()).date().isoformat()


def _apply(intake, belief, outcome, *, work_id, context, embed_texts):
    """Write the decision. The model never touches the store; this does."""
    action = outcome["action"]
    reasoning = str(outcome.get("reasoning") or "").strip()
    day = _outcome_day(context)

    # Attach BEFORE retiring: the evidence row is the reverse link we most want to survive, and
    # it is the idempotency fence for a redelivered receipt.
    intake.add_evidence(
        belief.belief_key, day, kind="did", relation=outcome["valence"], via="work_outcome",
        text=reasoning or f"Work object {work_id} ended {context.get('terminal', {}).get('status', '')}.",
        source_ref=f"work:{work_id}")

    if action == "resolve":
        intake.retire(belief.belief_key, day, f"work {work_id} satisfied this belief's condition: {reasoning}")
        return "resolved"
    if action == "revise":
        statement = str(outcome["statement"]).strip()
        intake.revise(belief.belief_key, day, {"statement": statement, "kind": belief.kind,
                                               "reasoning": f"work {work_id}: {reasoning}"},
                      embed_texts([statement])[0])
        return "revised"
    return "unchanged"


def _intake():
    from belief_engine.intake.run import app_store
    return app_store()


def _embedder():
    from app.assistant.embeddings.embedder import embed_texts
    return embed_texts


def _deliver_receipt(store, receipt) -> bool:
    from app.assistant.subconscious.concern_feedback import _last_user_reply
    from types import SimpleNamespace

    payload = receipt["payload"]
    work_id, context = receipt["work_id"], payload["context"]
    intake = _intake()

    pending, applied = [], []
    for key in payload["belief_refs"]:
        row = intake.get(key)
        if row is None:
            # A pre-cutover dotted key, or an id that never existed. Say so loudly rather than
            # wedge the outbox.
            logger.error("[belief_work_feedback] belief %r from work %s is not in the catalog — "
                         "outcome not recorded for it", key, work_id)
            continue
        if intake.has_evidence(key, f"work:{work_id}"):
            applied.append((key, "already_applied"))
            continue
        pending.append(SimpleNamespace(belief_key=row["id"], statement=row["statement"], status=row["status"],
                                       scope=row["scope"], kind=row["kind"], last_confirmed=row["last_confirmed"]))

    if pending:
        snapshot = SimpleNamespace(nodes={n["id"]: SimpleNamespace(**n) for n in payload["reply_nodes"]})
        decisions = _decide(pending, context, _last_user_reply(snapshot))
        embed_texts = _embedder()
        for belief in pending:
            applied.append((belief.belief_key,
                            _apply(intake, belief, decisions[belief.belief_key],
                                   work_id=work_id, context=context, embed_texts=embed_texts)))

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
