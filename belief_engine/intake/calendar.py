"""Which beliefs might belong on the day-planning calendar (owner, 2026-10-03).

The planner lays out each day from the beliefs the owner approved as calendar material. This proposes them: after
the nightly intake, `belief_engine::calendar_candidate` reads every active belief not yet looked at (calendar NULL)
and marks each `proposed` (with when it would appear and why) or `not_proposed`. The owner approves or declines the
proposals in the morning review; nothing here puts a belief on the calendar. The first run reads the whole catalog;
after that, each night's new beliefs.

Pages of PAGE_SIZE beliefs, one agent call each; an answer that does not decide every belief of its page exactly
once, by a key shown, with a `when` for every candidate, gets one correction, and a page still invalid raises.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

AGENT = "belief_engine::calendar_candidate"
PAGE_SIZE = 50


def _problems(data: Any, keys: List[str]) -> List[str]:
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["no decisions list"]
    out, seen = [], set()
    for d in data["decisions"]:
        key = str((d or {}).get("belief_key") or "").strip()
        if key not in keys:
            out.append(f"{key!r} is not one of the beliefs given")
        elif key in seen:
            out.append(f"{key} decided more than once")
        seen.add(key)
        if (d or {}).get("candidate") and not str(d.get("when") or "").strip():
            out.append(f"{key}: a candidate needs `when`")
    missing = [k for k in keys if k not in seen]
    if missing:
        out.append(f"not decided: {missing}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from belief_engine.intake.agents import _call, scope
    return _call(AGENT, payload, scope())


def tag_calendar_candidates(store, *, call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, int]:
    """Decide every active belief not yet looked at. Returns {proposed, not_proposed}."""
    call = call or _agent_call
    todo = store.calendar_undecided()
    counts = {"proposed": 0, "not_proposed": 0}
    for start in range(0, len(todo), PAGE_SIZE):
        page = todo[start:start + PAGE_SIZE]
        keys = [b["id"] for b in page]
        payload = {"beliefs": page, "correction": ""}
        data = call(payload)
        problems = _problems(data, keys)
        if problems:
            logger.warning("[calendar] invalid decisions, one correction: %s", problems)
            data = call({**payload, "correction": "; ".join(problems)})
            problems = _problems(data, keys)
            if problems:
                raise ValueError(f"calendar decisions still invalid after one correction: {problems}")
        for d in data["decisions"]:
            if d["candidate"]:
                store.set_calendar(d["belief_key"].strip(), "proposed", f"{d['when'].strip()}: {d['reason'].strip()}")
                counts["proposed"] += 1
            else:
                store.set_calendar(d["belief_key"].strip(), "not_proposed", d["reason"].strip())
                counts["not_proposed"] += 1
    logger.info("[calendar] %s", counts)
    return counts
