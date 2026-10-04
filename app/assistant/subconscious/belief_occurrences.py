"""Today's calendar handed to dayflow, one placed belief at a time (2026-10-03).

The day pass (subconscious/day_pass.py) places each selected belief at the times it applies today, with the
beliefs closest to it in meaning. Each placement is handed to dayflow here, by code, as an intake item
the steward must answer: `belief_occurrence:<belief_key>:<day>:<time>`, its summary the belief's own words
at its time, its related beliefs read with it. Work made from the item carries the belief in
`constraints.belief_refs` (dayflow_orchestrator/work_intake.py), so the work's outcome returns to the belief
(belief_engine/work_feedback.py) and, like everything done, reaches the brain, which attaches it to any
concern it bears on (docs/design/what_was_done_reaches_the_brain_2026-10-03.md).

Beliefs are not made into concerns (owner, 2026-10-03: "There is no good reason to make these beliefs into
concerns"). Until then each placed belief got a standing concern through the concern door, whose single rule
became each occurrence's task; the door merged beliefs about different acts and the rule misdescribed them.

Each placement is handed over once (its item id); one more than an hour past is not handed over, and an
"all day" one is handed over while the day lasts. The day runs from its boundary hour to the next day's
(pipelines/context.day_window_local), so a time before the boundary hour is after midnight.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

SOURCE_TYPE = "belief"


def item_id(belief_key: str, day: str, time: str) -> str:
    return f"belief_occurrence:{belief_key}:{day}:{'-'.join(time.lower().split())}"


def passed(day: str, when: str, now_utc: datetime) -> bool:
    """True when a clock-time placement today was more than an hour ago."""
    from app.assistant.pipelines.context import day_window_local
    from app.assistant.subconscious.day_pass import _clock_hour_minute
    if when == "all day":
        return False
    hour, minute = _clock_hour_minute(when)
    start, _ = day_window_local(day)
    at = start.replace(hour=hour, minute=minute)
    if at < start:
        at += timedelta(days=1)
    return (now_utc - at.astimezone(timezone.utc)).total_seconds() > 3600


def item_meta(line: Dict[str, Any], day: str, now_utc: datetime) -> Dict[str, Any]:
    """The intake item for one placement: the belief's words at its time, with its related beliefs."""
    when = line["time"]
    read_with = "\n".join(f"- {r['statement']}" for r in line.get("related") or [])
    return {
        "item_id": item_id(line["belief_key"], day, when),
        "source_type": SOURCE_TYPE, "event_type": "belief_occurrence",
        "created_at": now_utc.isoformat(),
        "summary": f"{'All day' if when == 'all day' else 'At ' + when} today ({day}): {line['statement']}",
        "belief_key": line["belief_key"], "occurrence_day": day, "occurrence_time": when,
        "read_with": read_with,
        "state": "artifact", "state_reason": "belief_occurrence", "evaluator_pending": True,
        "importance": "medium", "actionability": "actionable",
        "last_reviewed_at": now_utc.isoformat(), "cooldown_until": None, "linked_item_ids": [],
    }


def _handed_ids(day: str) -> set:
    """Item ids of every occurrence already handed over for the day."""
    from app.models.db_manager import get_db_manager
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID
    with get_db_manager().read_session() as session:
        raw = session.connection().connection.driver_connection
        rows = raw.execute("SELECT json_extract(metadata_json, '$.item_id') FROM unified_log_2026 WHERE source=? "
                           "AND room_id=? AND json_extract(metadata_json, '$.source_type') = ? "
                           "AND json_extract(metadata_json, '$.occurrence_day') = ?",
                           (DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID, SOURCE_TYPE, day)).fetchall()
    return {r[0] for r in rows}


def _write_item(meta: Dict[str, Any]) -> None:
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
    write_dayflow_items_batch([meta], caller="belief_occurrences")


def run_occurrence_handoffs(*, now_utc: Optional[datetime] = None, plan_connect=None,
                            handed_ids: Callable[[str], set] = None,
                            write_item: Callable[[Dict[str, Any]], None] = None,
                            poke: Callable[[], None] = None) -> Dict[str, Any]:
    """Hand every placement of today's calendar not yet handed over and not past to dayflow. Free when the
    day has no plan or nothing is due. The keyword arguments are test seams."""
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.subconscious import day_plan
    from app.assistant.subconscious.brain_trace import step
    from app.assistant.utils.time_utils import get_local_timezone
    now = now_utc or datetime.now(timezone.utc)
    day = boundary_date_local_str(now.astimezone(get_local_timezone()))
    plan = day_plan.load_plan(day, connect=plan_connect)
    if plan is None:
        return {"occurrences_handed": 0}
    lines: List[Dict[str, Any]] = [*plan["flags"], *(line for s in plan["slots"] for line in s["lines"])]
    handed = (handed_ids or _handed_ids)(day)
    due = [line for line in lines if item_id(line["belief_key"], day, line["time"]) not in handed
           and not passed(day, line["time"], now)]
    for line in due:
        meta = item_meta(line, day, now)
        (write_item or _write_item)(meta)
        step("handoff", "handed", subject=line["belief_key"], item_id=meta["item_id"], title=line["statement"],
             summary=meta["summary"], why="placed on today's calendar")
    if due:
        logger.info("[occurrences] handed %d placement(s) to dayflow: %s", len(due),
                    [item_id(line["belief_key"], day, line["time"]) for line in due])
        from app.assistant.subconscious.concern_handoff import _poke_dayflow
        (poke or _poke_dayflow)()
    return {"occurrences_handed": len(due)}
