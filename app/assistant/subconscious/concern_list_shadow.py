"""The brain's list for each concern, in shadow beside the live concern planner (2026-10-04).

Design: docs/design/brain_delegates_work_2026-10-04.md. The live concern decider writes "things that should be
done" with no one named, and the steward interprets them. The design has the brain keep entries instead: each a
piece of work naming who acts, the assistant's part and when, kept, modified or cancelled by its id as the concern
changes, so a changed plan adds only what is missing.

After the live planner writes a plan (subconscious/concern_planner.run_planners), the concern goes here with the
exact record the live decider read and what the planner looked up. `subconscious::concern_list_decider` answers
every entry the concern has on the list (keep, modify, cancel) and adds what is missing. Code checks the answer
(one correction round) and applies it to the list (subconscious/brain_list.py), every change recorded with its
cause. Nothing is handed to dayflow. Run by the brain's wake after its live stages, so a failure here never holds
up planning or handoffs; every concern is attempted, then any failure is raised.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::concern_list_decider"
_ACTIONS = ("keep", "modify", "cancel", "new")


def problems(data: Any, entry_ids: List[str]) -> List[str]:
    """Every entry on the list answered exactly once; modify and new name the outcome, who, the part and when."""
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return ["no entries list"]
    out, seen = [], []
    for a in data["entries"]:
        entry, action = str((a or {}).get("entry") or "").strip(), a.get("action")
        if action not in _ACTIONS:
            out.append(f"{entry}: action {action!r} is not one of {list(_ACTIONS)}")
            continue
        if action == "new":
            if entry != "new":
                out.append(f"a new entry is written as entry \"new\", got {entry!r}")
        elif entry not in entry_ids:
            out.append(f"{entry!r} is not an entry on the list")
            continue
        else:
            seen.append(entry)
        if action in ("modify", "new"):
            missing = [f for f in ("what", "who_acts", "assistant_part", "when") if not str(a.get(f) or "").strip()]
            if missing:
                out.append(f"{entry}: {action} names {', '.join(missing)}")
        if not str(a.get("why") or "").strip():
            out.append(f"{entry}: why is empty")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"answered more than once: {twice}")
    missing = [x for x in entry_ids if x not in seen]
    if missing:
        out.append(f"not answered: {missing}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="concern_list_shadow", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::concern_list_decider",
                                   actor_id="subconscious::concern_list_decider")


def decide(job: Dict[str, Any], *, now: datetime, call: Callable[[Dict[str, Any]], Any], connect=None) -> Dict[str, int]:
    """One concern: its record, what was looked up, its entries -> the list. Raises when still invalid."""
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.brain_trace import step
    from app.assistant.subconscious.concern_planner import concern_text
    concern, cid = job["concern"], job["concern"]["concern_id"]
    current = brain_list.entries(concern_id=cid, connect=connect)
    payload = {"now": now.isoformat(), "concern": concern_text(concern, job["bucket"]),
               "looked_up": job["looked_up"],
               "entries": [{k: e[k] for k in ("id", "what", "who_acts", "assistant_part", "when_local", "why")}
                           for e in current],
               "correction": None}
    ids = [e["id"] for e in current]
    data = call(payload)
    found = problems(data, ids)
    if found:
        step("list", "answer refused, one correction", subject=cid, problems=found)
        data = call({**payload, "correction": "; ".join(found)})
        found = problems(data, ids)
        if found:
            raise ValueError(f"concern list decider answer for {cid} still invalid after one correction: {found}")
    changed = brain_list.apply_concern(cid, data["entries"], cause=f"concern plan {now.isoformat()}", at=now,
                                       connect=connect)
    step("list", "decided (shadow)", subject=cid, title=concern.get("title"), list_changes=changed,
         entries=data["entries"])
    return changed


def run_concern_shadow(jobs: List[Dict[str, Any]], *, now_utc: Optional[datetime] = None, connect=None,
                       call: Callable[[Dict[str, Any]], Any] = None) -> Dict[str, Any]:
    """Every concern the live planner planned this wake. All are attempted; any failure is then raised."""
    if not jobs:
        return {"list_concerns": 0}
    from app.assistant.subconscious.brain_trace import trace
    now = now_utc or datetime.now(timezone.utc)
    failed: List[str] = []
    for job in jobs:
        cid = job["concern"]["concern_id"]
        try:
            with trace(stage="list", concern_id=cid):
                decide(job, now=now, call=call or _agent_call, connect=connect)
        except Exception as exc:
            logger.error("[concern_list_shadow] %s: %s", cid, exc, exc_info=True)
            failed.append(f"{cid}: {type(exc).__name__}: {exc}")
    if failed:
        raise RuntimeError(f"shadow list failed for {len(failed)} of {len(jobs)} concern(s): {failed}")
    return {"list_concerns": len(jobs)}
