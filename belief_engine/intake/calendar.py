"""Which beliefs might belong on the day-planning calendar (owner, 2026-10-03).

The planner lays out each day from the beliefs the owner approved as calendar material. This proposes them: after
the nightly intake, `belief_engine::calendar_candidate` reads every active belief not yet looked at (calendar NULL)
and marks each `proposed` (with when it would appear and why) or `not_proposed`. The owner approves or declines the
proposals in the morning review; nothing here puts a belief on the calendar. The first run reads the whole catalog;
after that, each night's new beliefs.

Pages of PAGE_SIZE beliefs, one agent call each. The agent's validate.py refuses an answer that does not decide every
belief of its page exactly once, by a key shown, with a `when` for every candidate; it is corrected once in
conversation and a page still invalid raises AgentOutputInvalid (agent_runtime/correction.py; until 2026-11-15 this
module re-asked with a correction string).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

AGENT = "belief_engine::calendar_candidate"
PAGE_SIZE = 50


def _agent_call(payload: Dict[str, Any]) -> Any:
    from belief_engine.intake.agents import _call, scope
    return _call(AGENT, payload, scope())


def tag_calendar_candidates(store, *, call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, int]:
    """Decide every active belief not yet looked at. Returns {proposed, not_proposed}."""
    call = call or _agent_call
    todo = store.calendar_undecided()
    counts = {"proposed": 0, "not_proposed": 0}
    for start in range(0, len(todo), PAGE_SIZE):
        data = call({"beliefs": todo[start:start + PAGE_SIZE]})
        for d in data["decisions"]:
            if d["candidate"]:
                store.set_calendar(d["belief_key"].strip(), "proposed", f"{d['when'].strip()}: {d['reason'].strip()}")
                counts["proposed"] += 1
            else:
                store.set_calendar(d["belief_key"].strip(), "not_proposed", d["reason"].strip())
                counts["not_proposed"] += 1
    logger.info("[calendar] %s", counts)
    return counts
