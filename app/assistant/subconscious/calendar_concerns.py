"""The day's beliefs become concerns (docs/design/concerns_as_state_2026-10-09.md, rule 7; 2026-10-09).

Owner, 2026-10-09: "This engine that builds the beliefs into the day, maybe they should just become concerns. then
really everything is a concern."; "Every concern that is created on the calendar would have an expiri of at most 2
hours or something reasonable determined by the agent creating them, all day event might have expiri at midnight.";
"right day pass would only ever edit these concerns. question is then since they are concerns brain can also edit
them."

The day pass (subconscious/day_pass.py) places the beliefs the owner approved for the calendar that apply today, each
with what it asks for today (`outcome`) and, for a clock time, until when it matters (`until`). Each placement is a
concern, raised here by code:
- its identity is its belief, its day and its time (a deterministic id), so it never goes through the concern door
  and nothing is merged into it;
- its source is the belief (`kind: belief`, the belief's statement), and it carries the belief (`belief_refs`), so
  the work made for it returns its outcome to the belief;
- it expires at its `until`, or at midnight for an all-day placement;
- the day pass edits only the concerns it raised: when it places the day again it updates what a placement says, and
  closes the concern of a placement it no longer makes. One the brain closed stays closed: it is never raised again
  for the same belief, day and time.
The brain reads them like any concern and may note, resolve or move their expiry. They have no planner pass (their
placement says what and when); the day's tasks decide their entries (`con:`), as for any concern.

Until then each placement was an item the day's tasks decided separately onto the list (`cal:<day>:<belief_key>`).
Days before CUTOVER_DAY keep those entries and their work.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

CUTOVER_DAY = "2026-10-10"      # the first day placed as concerns
_NAMESPACE = uuid.UUID("6f1d3c8e-2a47-4b4e-9d0a-3c5e7b9a1f20")


def concern_id(day: str, belief_key: str, time: str) -> str:
    """A calendar concern's identity: its belief, its day and its time."""
    return str(uuid.uuid5(_NAMESPACE, f"{day}|{belief_key}|{' '.join(time.lower().split())}"))


def _moment(day: str, clock: str) -> datetime:
    """A clock time on the planned day, in UTC; a time before the day's boundary hour is after midnight
    (pipelines/context.day_window_local)."""
    from app.assistant.pipelines.context import day_window_local
    from app.assistant.subconscious.day_pass import _clock_hour_minute
    hour, minute = _clock_hour_minute(clock)
    start, _ = day_window_local(day)
    at = start.replace(hour=hour, minute=minute)
    if at < start:
        at += timedelta(days=1)
    return at.astimezone(timezone.utc)


def expires_at(day: str, line: Dict[str, Any]) -> datetime:
    """Until when a placement matters: its `until` (the next one after its time), or midnight ending the day's date
    for an all-day placement."""
    from app.assistant.utils.time_utils import get_local_timezone
    if line["time"] == "all day":
        local = datetime.fromisoformat(day).replace(tzinfo=get_local_timezone()) + timedelta(days=1)
        return local.astimezone(timezone.utc)
    at, until = _moment(day, line["time"]), _moment(day, line["until"])
    return until if until > at else until + timedelta(days=1)


def from_plan(plan: Dict[str, Any], now_utc: datetime) -> List[Dict[str, Any]]:
    """The concerns the day's plan asks for, one per placement still ahead (its expiry in the future)."""
    day = plan["day"]
    out = []
    for line in [*plan["flags"], *(line for s in plan["slots"] for line in s["lines"])]:
        ends = expires_at(day, line)
        if ends <= now_utc:
            continue
        out.append({
            "concern_id": concern_id(day, line["belief_key"], line["time"]),
            "title": line["outcome"], "done_when": line["outcome"], "subject": "household",
            "kind": "anticipated_need", "severity": "low", "horizon": "today",
            "notes": f"{'All day' if line['time'] == 'all day' else 'At ' + line['time']}: {line['statement']}",
            "expires_at": ends.isoformat(), "belief_refs": [line["belief_key"]],
            "calendar": {"day": day, "belief_key": line["belief_key"], "time": line["time"]},
            "source": {"kind": "belief", "at": now_utc.isoformat(), "ref": f"belief:{line['belief_key']}",
                       "content": line["statement"]}})
    return out


def apply(plan: Dict[str, Any], now_utc: datetime, *, connect=None) -> Dict[str, int]:
    """Raise, update or close the day's calendar concerns to match its plan (from CUTOVER_DAY on)."""
    from app.assistant.subconscious import persist
    if plan["day"] < CUTOVER_DAY:
        return {"raised": 0, "updated": 0, "withdrawn": 0}
    counts = persist.apply_calendar_concerns(plan["day"], from_plan(plan, now_utc), now_utc, connect=connect)
    if any(counts.values()):
        logger.info("[calendar_concerns] %s: %s", plan["day"], counts)
    return counts
