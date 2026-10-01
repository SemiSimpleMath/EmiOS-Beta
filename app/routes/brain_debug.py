"""Owner-only /brain page: what the brain received, what each of its agents was sent and answered,
and what it decided.

What actually happened (recorded by subconscious/brain_trace.py at the one call path every agent
takes, so the prompts shown are the ones sent):
- matters   each matter the brain step handled: its events, every recorded call made for them
            (gate pages, the brain, the concern door) and the decisions applied.
- concerns  each open concern: brief and readiness, the brief writer's calls, every handoff to
            dayflow with the steward's answer and the steward call that gave it, the work serving it.
- calls     every recorded call of the brain's agents, filterable by agent; open one for its system
            prompt, user prompt and result.

What would be sent now (rendered by the agent's own input step and prompt builder; no model call):
- inbox    brain_events: every event, its gate route, the concerns it touched, the brain's decision.
- noticer  every context input the noticer gets this moment, and its rendered prompts.
- gate     the gate agent's rendered prompts for the latest events against the open concerns.
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


# ── what actually happened: recorded calls, matters, concerns (subconscious/brain_trace.py) ─────

@brain_debug_bp.route("/api/brain/calls")
def brain_calls_api():
    """Recorded model calls of the brain's agents, newest first (prompt sizes only)."""
    from app.assistant.subconscious import brain_trace
    agent = (request.args.get("agent") or "").strip() or None
    limit = min(int(request.args.get("limit") or 200), 2000)
    return jsonify({"calls": brain_trace.list_calls(agent=agent, agents=brain_trace.TRACED_AGENTS, limit=limit),
                    "agents": sorted(brain_trace.TRACED_AGENTS)})


@brain_debug_bp.route("/api/brain/calls/<call_id>")
def brain_call_api(call_id: str):
    """One recorded call in full: the system and user prompts as sent, and the result or error."""
    from app.assistant.subconscious import brain_trace
    return jsonify(brain_trace.get_call(call_id))


@brain_debug_bp.route("/api/brain/matters")
def brain_matters_api():
    """What the brain did, one matter at a time, newest first: the events it read, every recorded
    call made for them (gate, brain, concern door), and the decisions applied."""
    import json
    from app.assistant.subconscious import brain_inbox, brain_step, brain_trace
    limit = min(int(request.args.get("limit") or 50), 500)
    brain_step.ensure_schema()
    with brain_inbox._connect(False) as c:
        matters = [dict(r) for r in c.execute("SELECT * FROM brain_matters ORDER BY id DESC LIMIT ?", (limit,))]
        out = []
        for m in matters:
            ids = json.loads(m["event_ids"])
            marks = ",".join("?" * len(ids))
            events = [dict(r) for r in c.execute(
                f"SELECT id, source, source_ref, occurred_at, room_id, speaker, text, gate_status, route, "
                f"concern_ids, gate_reasoning FROM brain_events WHERE id IN ({marks}) ORDER BY occurred_at", ids)]
            out.append({**m, "event_ids": ids, "concern_ids": json.loads(m["concern_ids"]),
                        "decisions": json.loads(m["decisions"]) if m["decisions"] else None,
                        "admitted": json.loads(m["admitted"]) if m["admitted"] else None,
                        "events": events, "calls": brain_trace.calls_for_events(ids)})
    return jsonify({"matters": out})


@brain_debug_bp.route("/api/brain/concerns")
def brain_concerns_api():
    """Every open concern with what the brain knows and decided: its brief and readiness, its journal,
    the brief writer's calls, each handoff to dayflow with the steward's answer and the steward call
    that gave it, and the work that serves it."""
    import json
    from app.assistant.subconscious import brain_trace, work_links
    from app.assistant.subconscious.concern_brief import basis
    from app.assistant.subconscious.concern_handoff import _existing_items
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    rows = work_links._rows()
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            cid = c["concern_id"]
            handoffs = [{**i, "steward_calls": brain_trace.steward_calls_mentioning(i["item_id"])}
                        for i in sorted(_existing_items(cid), key=lambda i: i.get("created_at") or "")]
            out.append({
                "concern_id": cid, "status": bucket, "title": c.get("title"), "subject": c.get("subject"),
                "origin": c.get("origin"), "done_when": c.get("done_when"), "owner_request": c.get("owner_request"),
                "notes": c.get("notes"), "journal": c.get("reinforcement_notes") or "",
                "brief": c.get("brief"), "brief_current": (c.get("brief") or {}).get("basis") == basis(c, bucket),
                "brief_error": c.get("brief_error"), "brief_calls": brain_trace.calls_for_concern(cid),
                "handoffs": handoffs,
                "work": [{"work_id": r["id"], "status": r["status"], "title": r["title"]} for r in rows
                         if work_links._cites(r["constraints"].get("concern_refs") or [], cid)],
            })
    return jsonify({"concerns": out})
