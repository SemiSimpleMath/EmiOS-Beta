"""Owner-only /brain test page: everything the brain receives, and the prompts its agents see.

Three views, all read-only:
- inbox    brain_events: every event, its gate route, the concerns it touched, the gate's
           reasoning and the noticer's decision.
- noticer  every context input the noticer gets this moment (the same build_noticer_context a
           run uses, with the unconsumed reports), each with its size, and the system + user
           prompts the noticer agent would be sent, rendered by the agent itself.
- gate     the gate agent's rendered prompts for the latest events against the open concerns.

Rendering goes through the agent's own input step and prompt builder (what a real run does), so
the page shows exactly what the model gets. No model is called and nothing is written.
"""
from __future__ import annotations

from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template, request

from app.assistant.utils.logging_config import get_logger
from app.routes._security import reject_if_not_local

logger = get_logger(__name__)

brain_debug_bp = Blueprint("brain_debug", __name__)
brain_debug_bp.before_request(reject_if_not_local)


def _render_prompts(agent_name: str, context: Dict[str, Any], scope_id: str) -> Dict[str, str]:
    """The system and user prompts `agent_name` would be sent for `context`."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.utils.pydantic_classes import Message

    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        raise RuntimeError(f"agent {agent_name!r} not found")
    scope = load_scope_for_source(
        kind="subsystem", source_id="subconscious", actor_id="brain_debug_page",
        identity_overrides={"owner_id": "system", "scope_id": scope_id, "surface": "internal"})
    message = Message(agent_input=context, scope_context=scope)
    agent._update_blackboard_state(message)
    messages = agent.construct_prompt(message)

    def text(role: str) -> str:
        parts = []
        for m in messages:
            if m.get("role") != role:
                continue
            content = m.get("content")
            parts.append(content if isinstance(content, str) else str(content))
        return "\n\n".join(parts)

    return {"system": text("system"), "user": text("user")}


@brain_debug_bp.route("/brain")
def brain_page():
    return render_template("brain.html")


@brain_debug_bp.route("/api/brain/inbox")
def brain_inbox_api():
    """Newest events first. Filters: route (concern | new_matter | none | failed | pending)."""
    from app.assistant.subconscious import brain_inbox
    route = (request.args.get("route") or "").strip()
    limit = min(int(request.args.get("limit") or 200), 2000)
    where, params = [], []
    if route in ("pending", "failed"):
        where.append("gate_status = ?"); params.append(route)
    elif route:
        where.append("route = ?"); params.append(route)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    brain_inbox.ensure_schema()
    with brain_inbox._connect(False) as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM brain_events{clause} ORDER BY occurred_at DESC, id DESC LIMIT ?", (*params, limit))]
        counts = {r[0] or "pending": r[1] for r in c.execute(
            "SELECT CASE WHEN gate_status='routed' THEN route ELSE gate_status END, COUNT(*) FROM brain_events "
            "GROUP BY 1")}
    import json
    from app.assistant.subconscious import brain_step
    from app.assistant.subconscious.concern_store import load_register
    titles = {c.get("concern_id"): c.get("title") for b, items in load_register().items()
              if isinstance(items, list) for c in items}
    # The brain's decision on each event, from the matter that read it.
    brain_step.ensure_schema()
    decided, failed = {}, {}
    with brain_inbox._connect(False) as c:
        for status, decisions, error in c.execute("SELECT status, decisions, error FROM brain_matters"):
            if status == "applied":
                decided.update(json.loads(decisions)["events"])
    with brain_inbox._connect(False) as c:
        for event_ids, error in c.execute("SELECT event_ids, error FROM brain_matters WHERE status='failed'"):
            failed.update({i: error for i in json.loads(event_ids)})
    for r in rows:
        r["concern_ids"] = json.loads(r["concern_ids"]) if r.get("concern_ids") else []
        r["concerns"] = [{"id": cid, "title": titles.get(cid, "(not in the register)")} for cid in r["concern_ids"]]
        d = decided.get(r["source_ref"])
        r["brain_decision"], r["brain_reason"] = (d["decision"], d["reason"]) if d else (None, None)
        r["brain_error"] = failed.get(r["id"])
    return jsonify({"events": rows, "counts": counts})


@brain_debug_bp.route("/api/brain/noticer")
def brain_noticer_api():
    """Every noticer context input, sized, plus the rendered prompts. Builds live context (reads the
    calendar), so it takes a few seconds."""
    from app.assistant.subconscious import brain_step
    from app.assistant.subconscious.context_builder import build_noticer_context
    context = build_noticer_context(trigger_mode="daily")
    inputs: List[Dict[str, Any]] = [{"key": k, "chars": len(str(v)), "text": str(v)} for k, v in context.items()]
    prompts = _render_prompts("subconscious::noticer", context, "subconscious::noticer")
    return jsonify({"inputs": inputs, "reports_unconsumed": len(brain_step.pending_events()), **prompts})


@brain_debug_bp.route("/api/brain/gate")
def brain_gate_api():
    """The gate's rendered prompts for the pending events, or the latest N when none are pending."""
    from app.assistant.subconscious import brain_inbox
    from app.assistant.subconscious.concern_store import load_register
    from app.assistant.subconscious.gate import build_payload, open_concerns
    n = min(int(request.args.get("n") or 10), 200)
    events = brain_inbox.pending()
    source = "pending"
    if not events:
        with brain_inbox._connect(False) as c:
            events = [dict(r) for r in c.execute(
                "SELECT * FROM brain_events ORDER BY occurred_at DESC, id DESC LIMIT ?", (n,))][::-1]
        source = f"latest {len(events)} (none pending)"
    payload = build_payload(events, open_concerns(load_register()))
    prompts = _render_prompts("subconscious::gate", payload, "subconscious::gate")
    return jsonify({"events_source": source, "event_count": len(events),
                    "concern_count": len(payload["open_concerns"]), **prompts})
