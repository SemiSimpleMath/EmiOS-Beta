"""The concern planner: what a matter concern needs now, as its entries on the brain's list (2026-10-02; entries
2026-10-05).

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

Since 2026-10-05 the decider is `subconscious::concern_tasks` (docs/design/brain_delegates_work_2026-10-04.md, "Two
task deciders, one list"): the things that should be done are the concern's entries on the brain's list
(subconscious/brain_list.py), each one outcome with its deadline and constraints, the assistant's part and when.
It reads the concern's entries now, and answers only the changes: new entries, and entries modified or cancelled
by id; an entry it leaves out stays as it is. The plan keeps its shape for its readers (the concern view every
dayflow agent reads, the stories, the handoff): `todo` is the concern's open entries after the change. The shadow
list decider that ran beside the live decider (2026-10-04) is gone.

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
_DECIDER = "subconscious::concern_tasks"
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
    view = {"concern_id": concern["concern_id"], "title": concern.get("title"), "status": bucket, "severity": concern.get("severity"),
            "done_when": concern.get("done_when"), "owner_words": (concern.get("owner_request") or {}).get("words"),
            "brief": concern.get("brief"), "attempts": concern_brief.attempts(concern), "brief_current": True}
    from app.assistant.subconscious.persist import journal_for_prompt
    journal = journal_for_prompt(concern.get("reinforcement_notes") or "")
    return render_view("concern_brief", concerns=[view]).strip() + (f"\n  journal:\n{journal}" if journal else "")


def problems(data: Any, now_utc: datetime, entry_ids: List[str]) -> List[str]:
    """act_now changes at least one entry (new or modified), and only act_now adds or modifies; a modify or a cancel
    names an entry on the list, a new one is written as entry "new"; new and modify name the outcome, the
    assistant's part and when; every change says why; hold has a future time."""
    from app.assistant.subconscious.concern_brief import hold_until_utc
    if not isinstance(data, dict):
        return ["no decision"]
    decision = data.get("decision")
    if decision not in _DECISIONS:
        return [f"decision {decision!r} is not one of {list(_DECISIONS)}"]
    out = [] if str(data.get("why") or "").strip() else ["why is empty"]
    changes = data.get("entries") or []
    adds = [e for e in changes if (e or {}).get("action") in ("new", "modify")]
    if decision == "act_now" and not adds:
        out.append("act_now needs at least one new or modified entry")
    if decision != "act_now" and adds:
        out.append(f"{decision} adds or modifies entries; only act_now does")
    seen = []
    for e in changes:
        action, entry = e.get("action"), str(e.get("entry") or "").strip()
        if action not in ("new", "modify", "cancel"):
            out.append(f"{entry}: action {action!r} is not new, modify or cancel")
            continue
        if action == "new":
            if entry != "new":
                out.append(f'a new entry is written as entry "new", got {entry!r}')
        elif entry not in entry_ids:
            out.append(f"{entry!r} is not an entry on the list")
            continue
        else:
            seen.append(entry)
        if action in ("new", "modify"):
            missing = [f for f in ("what", "assistant_part", "when") if not str(e.get(f) or "").strip()]
            if missing:
                out.append(f"{entry}: {action} names {', '.join(missing)}")
        if not str(e.get("why") or "").strip():
            out.append(f"{entry}: why is empty")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"changed more than once: {twice}")
    if decision == "hold":
        try:
            hold_until_utc(data.get("hold_until"), now_utc)
        except ValueError as exc:
            out.append(f"hold needs hold_until as a future local YYYY-MM-DD HH:MM: {exc}")
    return out


def _invoke(task: str, information: str) -> Optional[Dict[str, Any]]:
    """Run concern_planner_manager on one concern (`task`: the concern as both agents read it; `information`: its
    entries on the brain's list); the decider's answer from the manager's blackboard."""
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


def write_plan(concern: Dict[str, Any], bucket: str, *, invoke: Optional[Callable[[str, str], Any]] = None,
               list_connect=None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The plan for one matter concern and the changes to its entries on the brain's list; raises when the
    decider's answer is invalid. The plan's `todo` is the concern's open entries after the changes."""
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.concern_brief import hold_until_utc
    now = datetime.now(timezone.utc)
    from app.assistant.dayflow_orchestrator.brain_list_intake import work_of
    current = brain_list.entries(concern_id=concern["concern_id"], connect=list_connect)
    work = work_of([e["id"] for e in current])
    entries_text = _ENV.get_template("shared/brain/concern_entries.j2").render(
        entries=[{**e, "work": work.get(e["id"])} for e in current]).strip()
    data = (invoke or _invoke)(concern_text(concern, bucket), entries_text)
    found = problems(data, now, [e["id"] for e in current])
    if found:
        raise ValueError(f"concern_tasks answer invalid: {found}")
    changes = [{k: str(e.get(k) or "").strip() for k in ("entry", "action", "what", "assistant_part", "when", "why")}
               for e in data.get("entries") or []]
    cancelled = {c["entry"] for c in changes if c["action"] == "cancel"}
    modified = {c["entry"]: c for c in changes if c["action"] == "modify"}
    todo = [modified.get(e["id"], e)["what"] for e in current if e["id"] not in cancelled] + \
           [c["what"] for c in changes if c["action"] == "new"]
    plan = {"decision": data["decision"], "todo": todo, "why": data["why"],
            "hold_until": data.get("hold_until") if data["decision"] == "hold" else None}
    if plan["decision"] == "hold":
        plan["hold_until_utc"] = hold_until_utc(plan["hold_until"], now).isoformat()
    return plan, changes


def run_planners(*, invoke=None, register_connect=None, list_connect=None) -> Dict[str, Any]:
    """Plan every matter concern that needs it, and apply each plan's changes to the concern's entries on the
    brain's list, then store the plan. Free when none does."""
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
                plan, changes = write_plan(concern, bucket, invoke=invoke, list_connect=list_connect)
        except Exception as exc:
            logger.error("[plan] could not plan %s; not retried until it changes: %s", cid, exc, exc_info=True)
            step("plan", "failed: not retried until the brief changes", subject=cid,
                 error=f"{type(exc).__name__}: {exc}")
            persist.set_concern_plan(cid, from_brief, error=f"{type(exc).__name__}: {exc}", connect=register_connect)
            failed += 1
            continue
        from app.assistant.subconscious import brain_list
        now = datetime.now(timezone.utc)
        listed = {}

        def apply_entries(cid=cid, changes=changes, now=now):
            listed.update(brain_list.apply_concern(cid, changes, cause=f"concern plan {now.isoformat()}", at=now,
                                                   connect=list_connect))
        # The entries land before the plan is stored (persist.set_concern_plan): a plan is current only once
        # its entries are on the list. A failure applying them stores nothing: the concern is planned again.
        try:
            stored = persist.set_concern_plan(cid, from_brief, plan=plan, before_store=apply_entries,
                                              connect=register_connect)
        except Exception as exc:
            logger.error("[plan] %s: its entries could not be put on the list; no plan stored, planned again next "
                         "pass: %s", cid, exc, exc_info=True)
            step("plan", "entries not applied: no plan stored, planned again", subject=cid,
                 error=f"{type(exc).__name__}: {exc}")
            failed += 1
            continue
        if stored:
            step("plan", "written", subject=cid, plan=plan, entries=changes, list_changes=listed)
            written += 1
        else:
            step("plan", "not stored: the brief changed while it was planned", subject=cid)
    logger.info("[plan] written=%d failed=%d", written, failed)
    return {"plans_written": written, "plans_failed": failed}
