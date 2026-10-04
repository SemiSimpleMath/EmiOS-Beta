"""The concern planner: what a matter concern needs now, as things that should be done (2026-10-02).

The brief (subconscious/concern_brief.py) makes a concern's record readable. For a matter concern (one
that is settled when its matter is done) the planner then works out what it needs now: a read-only
planner looks things up (the email threads and calendar entries the concern names, reminders and to-dos,
the work done for it, pods, the knowledge graph) and a decider writes the decision (act now, hold until a
date, no action) and the things that should be done, as bullet points under the concern
(`concern["plan"]`). It changes nothing in the world. Consumers read the bullets and decide how: dayflow
gets them in the concern's handoff (subconscious/concern_handoff.py).

Owner, 2026-10-02: "it breaks it into bullet points under the concern like: these are things that should
be done. its up to consumers to interpret those." It replaced the brief's own readiness decision for
matter concerns; a standing concern (a recurring part of the day) keeps its brief's per-occurrence rule.

It runs for a matter concern whose brief is current and that has no plan written from that brief, or
whose plan holds until a time that has passed. A plan that could not be written carries `plan_error` with
the same basis and is not retried until the concern changes. Run by the brain's wake after the briefs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MANAGER = "concern_planner_manager"
_DECIDER = "subconscious::concern_decider"
_DECISIONS = ("act_now", "hold", "no_action")


def _brief_basis(concern: Dict[str, Any]) -> Optional[str]:
    return (concern.get("brief") or {}).get("basis")


def _hold_passed(plan: Dict[str, Any], now: datetime) -> bool:
    when = plan.get("hold_until_utc")
    return plan.get("decision") == "hold" and bool(when) and datetime.fromisoformat(when) <= now


def stale(register: Dict[str, Any], now_utc: Optional[datetime] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """Open matter concerns whose current brief has no plan written from it, or whose plan's hold has
    passed; minus those whose plan already failed on this brief. (bucket, concern)."""
    from app.assistant.subconscious.concern_brief import basis
    now = now_utc or datetime.now(timezone.utc)
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            b = _brief_basis(c)
            if b is None or b != basis(c, bucket):
                continue                                   # no current brief yet: the brief comes first
            if (c.get("plan_error") or {}).get("basis") == b:
                continue
            plan = c.get("plan") or {}
            if plan.get("basis") == b and not _hold_passed(plan, now):
                continue
            out.append((bucket, c))
    return out


def stale_reason(concern: Dict[str, Any]) -> str:
    """Why a plan is written again, for the run record."""
    plan = concern.get("plan") or {}
    if not plan:
        return "no plan yet"
    if plan.get("basis") == _brief_basis(concern):
        return "its hold passed"
    return "its brief changed"


def next_hold_at(register: Dict[str, Any], now_utc: datetime) -> Optional[datetime]:
    """The earliest future time a matter concern's current plan holds until, or None."""
    times = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            plan = c.get("plan") or {}
            when = plan.get("hold_until_utc")
            if plan.get("decision") == "hold" and when and plan.get("basis") == _brief_basis(c):
                at = datetime.fromisoformat(when)
                if at > now_utc:
                    times.append(at)
    return min(times) if times else None


def concern_text(concern: Dict[str, Any], bucket: str) -> str:
    """The concern as the planner and the decider read it: the same view every agent working on it reads,
    with its journal (handoff answers, the owner's replies)."""
    from app.assistant.dayflow_orchestrator.work_context import render_view
    from app.assistant.subconscious import concern_brief
    view = {"concern_id": concern["concern_id"], "title": concern.get("title"), "status": bucket,
            "done_when": concern.get("done_when"), "owner_words": (concern.get("owner_request") or {}).get("words"),
            "brief": concern.get("brief"), "attempts": concern_brief.attempts(concern), "brief_current": True}
    journal = (concern.get("reinforcement_notes") or "").strip()
    return render_view("concern_brief", concerns=[view]).strip() + (f"\n  journal:\n{journal}" if journal else "")


def problems(data: Any, now_utc: datetime) -> List[str]:
    from app.assistant.subconscious.concern_brief import hold_until_utc
    if not isinstance(data, dict):
        return ["no decision"]
    decision = data.get("decision")
    if decision not in _DECISIONS:
        return [f"decision {decision!r} is not one of {list(_DECISIONS)}"]
    out = [] if str(data.get("why") or "").strip() else ["why is empty"]
    todo = [t for t in data.get("todo") or [] if str(t).strip()]
    if decision == "act_now" and not todo:
        out.append("act_now needs at least one thing that should be done")
    if decision != "act_now" and todo:
        out.append(f"{decision} lists things to do; only act_now does")
    if decision == "hold":
        try:
            hold_until_utc(data.get("hold_until"), now_utc)
        except ValueError as exc:
            out.append(f"hold needs hold_until as a future local YYYY-MM-DD HH:MM: {exc}")
    return out


def _invoke(task: str, information: str) -> Optional[Dict[str, Any]]:
    """Run concern_planner_manager on one concern (`task`: the concern as both agents read it); the
    decider's answer from the manager's blackboard."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.utils.pydantic_classes import Message
    scope = load_scope_for_source(
        kind="room", source_id="dayflow_orchestrator", actor_id="concern_planner",
        identity_overrides={"scope_id": "concern_planner_inline", "owner_id": "dayflow_orchestrator",
                            "surface": "system", "room_id": "dayflow_orchestrator"})
    manager = DI.multi_agent_manager_factory.create_manager(_MANAGER)
    DI.manager_invoker.invoke(manager, Message(task=task, information=information, scope_context=scope))
    from app.assistant.subconscious.brain_trace import current, step
    for m in manager.blackboard.get_messages():
        if getattr(m, "data_type", None) == "tool_result":
            step("plan", "tool result", subject=current("concern_id"), result_type=list(m.sub_data_type or []),
                 content=m.content)
    for m in reversed(manager.blackboard.get_messages()):
        if str(getattr(m, "sender", "") or "").endswith(_DECIDER.split("::")[-1]):
            data = getattr(m, "data", None)
            if isinstance(data, dict) and "decision" in data:
                return data
    return None


def write_plan(concern: Dict[str, Any], bucket: str, *,
               invoke: Optional[Callable[[str, str], Any]] = None) -> Dict[str, Any]:
    """The plan for one matter concern; raises when the decider's answer is invalid."""
    from app.assistant.subconscious.concern_brief import hold_until_utc
    now = datetime.now(timezone.utc)
    data = (invoke or _invoke)(concern_text(concern, bucket), "")
    found = problems(data, now)
    if found:
        raise ValueError(f"concern decider answer invalid: {found}")
    plan = {"decision": data["decision"], "todo": [str(t).strip() for t in data.get("todo") or [] if str(t).strip()],
            "why": data["why"], "hold_until": data.get("hold_until") if data["decision"] == "hold" else None}
    if plan["decision"] == "hold":
        plan["hold_until_utc"] = hold_until_utc(plan["hold_until"], now).isoformat()
    return plan


def run_planners(*, invoke=None, register_connect=None) -> Dict[str, Any]:
    """Plan every matter concern that needs it. Free when none does."""
    from app.assistant.subconscious import persist
    from app.assistant.subconscious.concern_store import load_register
    todo = stale(load_register(connect=register_connect))
    if not todo:
        return {"plans_written": 0, "plans_failed": 0}
    written = failed = 0
    for bucket, concern in todo:
        from_brief = _brief_basis(concern)
        cid = concern["concern_id"]
        from app.assistant.subconscious.brain_trace import step, trace
        step("plan", "stale", subject=cid, title=concern.get("title"), reason=stale_reason(concern))
        try:
            with trace(stage="plan", concern_id=cid):
                plan = write_plan(concern, bucket, invoke=invoke)
        except Exception as exc:
            logger.error("[plan] could not plan %s; not retried until it changes: %s", cid, exc, exc_info=True)
            step("plan", "failed: not retried until the brief changes", subject=cid,
                 error=f"{type(exc).__name__}: {exc}")
            persist.set_concern_plan(cid, from_brief, error=f"{type(exc).__name__}: {exc}", connect=register_connect)
            failed += 1
            continue
        if persist.set_concern_plan(cid, from_brief, plan=plan, connect=register_connect):
            step("plan", "written", subject=cid, plan=plan)
            written += 1
        else:
            step("plan", "not stored: the brief changed while it was planned", subject=cid)
    logger.info("[plan] written=%d failed=%d", written, failed)
    return {"plans_written": written, "plans_failed": failed}
