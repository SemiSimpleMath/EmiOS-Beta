"""The day's beliefs become concerns (docs/design/concerns_as_state_2026-10-09.md, rule 7; 2026-10-09).

Owner, 2026-10-09: "This engine that builds the beliefs into the day, maybe they should just become concerns. then
really everything is a concern."; "Every concern that is created on the calendar would have an expiri of at most 2
hours or something reasonable determined by the agent creating them, all day event might have expiri at midnight.";
"right day pass would only ever edit these concerns. question is then since they are concerns brain can also edit
them."

The day pass (subconscious/day_pass.py) judges each belief the owner approved for the calendar against today and
writes today's plan: one line per belief that applies, at each time it applies, with what it asks for today
(`outcome`) and, for a clock time, until when it matters (`until`). The plan's lines still ahead are made into the
day's placements by `subconscious::day_placements`: one placement per thing the day holds, with every line about it
(owner, 2026-10-10: "we already had an agent that looks at all the beliefs and picks out the ones that apply today";
until the plan became data on 2026-10-02 the writer wrote one line per moment citing several beliefs). Each placement
is a concern, raised here by code:
- the agent reads the concerns already made from today's plan and names the one a placement continues, so one thing
  keeps one concern all day, through a moved time, a reworded outcome or a changed set of beliefs; a placement that
  continues none is a new concern with a new id. It never goes through the concern door;
- its source is its beliefs (`kind: belief`, their statements), and it carries them (`belief_refs`), so the work made
  for it returns its outcome to each belief;
- it expires at its `until`, or at midnight for an all-day placement;
- the day pass edits only the concerns it raised: a continued one takes the placement's outcome, time, expiry and
  beliefs, and an open one no placement continues is closed (withdrawn). One the brain closed stays closed.
The brain reads them like any concern and may note or resolve them; an expiry the brain moved is set again by the
next placement of the day. They have no planner pass (their placement says what and when); the day's tasks decide
their entries (`con:`), as for any concern.

Until 2026-10-10 each plan line was its own concern, its identity its belief, day and time: beliefs about one moment
became several concerns (three for the morning dog walk), and a time the day pass moved closed the concern and raised
another, whose entry the day's tasks did not make while the closed one's work still looked in motion (the evening air
conditioner, 2026-10-10). Until 2026-10-09 each placement was an item the day's tasks decided onto the list
(`cal:<day>:<belief_key>`); days before CUTOVER_DAY keep those entries and their work.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

CUTOVER_DAY = "2026-10-10"      # the first day placed as concerns
_AGENT = "subconscious::day_placements"
_WITHDRAWN = "the day pass no longer places it today"


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


def lines_ahead(plan: Dict[str, Any], now_utc: datetime) -> List[Dict[str, Any]]:
    """The plan's lines that still matter (their expiry in the future), in time order as stored."""
    day = plan["day"]
    return [line for line in [*plan["flags"], *(line for s in plan["slots"] for line in s["lines"])]
            if expires_at(day, line) > now_utc]


def day_concerns(register: Dict[str, Any], day: str) -> List[Tuple[str, Dict[str, Any]]]:
    """The concerns made from the day's plan that a placement may continue: the open ones and the ones the brain
    closed (a withdrawn one is the day pass's own and is not offered again)."""
    return [(b, c) for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []
            if (c.get("calendar") or {}).get("day") == day and c.get("resolution_reason") != _WITHDRAWN]


def _clock(iso: str) -> str:
    from app.assistant.utils.time_utils import get_local_timezone
    return datetime.fromisoformat(iso).astimezone(get_local_timezone()).strftime("%I:%M %p").lstrip("0")


def build_payload(plan: Dict[str, Any], lines: List[Dict[str, Any]], concerns: List[Tuple[str, Dict[str, Any]]],
                  context: Dict[str, Any]) -> Dict[str, Any]:
    """What the placements agent reads: the day, the lines still ahead (L) and today's concerns (P)."""
    f = context["day_facts"]
    return {
        "day_text": f"{f['weekday']}, {f['date']}", "now_local": context["now_local"],
        "daily_context": context["daily_context"],
        "lines": [{"label": f"L{i}", "time": line["time"], "until": line["until"], "outcome": line["outcome"],
                   "statement": line["statement"]} for i, line in enumerate(lines, 1)],
        "concerns": [{"label": f"P{i}", "time": c["calendar"]["time"],
                      "until": "" if c["calendar"]["time"] == "all day" else _clock(c["expires_at"]),
                      "title": c["title"], "state": "open" if b in ("active", "addressing") else "closed by the brain",
                      "statements": c["source"]["content"].split("\n")} for i, (b, c) in enumerate(concerns, 1)]}


def _problems(data: Dict[str, Any], line_labels: List[str], concern_labels: List[str]) -> List[str]:
    """What is wrong with the placements: a line label or concern label not given, a line in no placement or in two, a
    concern continued twice, an empty outcome, a time that is neither a clock time nor 'all day', or a clock time
    without a clock time it matters until."""
    from app.assistant.subconscious.day_pass import _clock_hour_minute
    out, used, continued = [], [], []
    for n, p in enumerate(data.get("placements") or [], 1):
        refs = [str(x).strip() for x in p.get("lines") or []]
        if not refs:
            out.append(f"placement {n}: name the lines it is made of")
        for r in refs:
            if r not in line_labels:
                out.append(f"placement {n}: {r!r} is not one of the lines given")
        used += refs
        cont = str(p.get("continues") or "").strip()
        if cont:
            if cont not in concern_labels:
                out.append(f"placement {n}: {cont!r} is not one of today's concerns")
            continued.append(cont)
        if not str(p.get("outcome") or "").strip():
            out.append(f"placement {n}: say what it asks for today, as an outcome")
        when, until = str(p.get("time") or "").strip(), str(p.get("until") or "").strip()
        if when.lower() == "all day":
            continue
        if _clock_hour_minute(when) is None:
            out.append(f"placement {n}: time {when!r} is not a clock time like '9:00 PM', or 'all day'")
        elif _clock_hour_minute(until) is None:
            out.append(f"placement {n}: at {when}, until {until!r} is not a clock time like '11:00 PM'")
    for label in line_labels:
        if used.count(label) != 1:
            out.append(f"{label}: put it in exactly one placement" if used.count(label) else
                       f"{label}: in no placement; every line is in one")
    for label in sorted(set(continued)):
        if continued.count(label) > 1:
            out.append(f"{label}: continued by {continued.count(label)} placements; at most one continues it")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="day_placements", agent_name=_AGENT, context=payload,
                                   scope_id=_AGENT, actor_id=_AGENT)


def placements(payload: Dict[str, Any], *, call: Callable[[Dict[str, Any]], Any]) -> List[Dict[str, Any]]:
    """The agent's placements, checked; one correction, then a loud failure."""
    from app.assistant.subconscious.brain_trace import step
    line_labels = [x["label"] for x in payload["lines"]]
    concern_labels = [x["label"] for x in payload["concerns"]]
    data = call(payload)
    problems = _problems(data if isinstance(data, dict) else {}, line_labels, concern_labels)
    if problems:
        logger.warning("[calendar_concerns] invalid placements, one correction: %s", problems)
        step("day_pass", "placements refused, one correction", problems=problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data if isinstance(data, dict) else {}, line_labels, concern_labels)
        if problems:
            raise ValueError(f"day placements still invalid after one correction: {problems}")
    return data["placements"]


def desired(day: str, answer: List[Dict[str, Any]], lines: List[Dict[str, Any]],
            concerns: List[Tuple[str, Dict[str, Any]]], now_utc: datetime) -> List[Dict[str, Any]]:
    """The concerns the placements ask for: a continued one keeps its id, a new one gets a new id."""
    by_line = {f"L{i}": line for i, line in enumerate(lines, 1)}
    by_concern = {f"P{i}": c for i, (_, c) in enumerate(concerns, 1)}
    out = []
    for p in answer:
        made_of = [by_line[str(r).strip()] for r in p["lines"]]
        keys = list(dict.fromkeys(line["belief_key"] for line in made_of))
        statements = list(dict.fromkeys(line["statement"] for line in made_of))
        when = "all day" if str(p["time"]).strip().lower() == "all day" else " ".join(str(p["time"]).split())
        until = "" if when == "all day" else " ".join(str(p["until"]).split())
        outcome = " ".join(str(p["outcome"]).split())
        cont = str(p.get("continues") or "").strip()
        out.append({
            "concern_id": by_concern[cont]["concern_id"] if cont else str(uuid.uuid4()),
            "title": outcome, "done_when": outcome, "subject": "household",
            "kind": "anticipated_need", "severity": "low", "horizon": "today",
            "notes": f"{'All day' if when == 'all day' else 'At ' + when}: " + " ".join(statements),
            "expires_at": expires_at(day, {"time": when, "until": until}).isoformat(), "belief_refs": keys,
            "calendar": {"day": day, "time": when},
            "source": {"kind": "belief", "at": now_utc.isoformat(), "ref": ",".join(f"belief:{k}" for k in keys),
                       "content": "\n".join(statements)}})
    return out


def apply(plan: Dict[str, Any], now_utc: datetime, *, context: Dict[str, Any],
          call: Optional[Callable[[Dict[str, Any]], Any]] = None, connect=None) -> Dict[str, int]:
    """Raise, update or close the day's calendar concerns to match its plan (from CUTOVER_DAY on). `context` is what
    the day pass read (day_facts, now_local, daily_context); `call` and `connect` are test seams."""
    from app.assistant.subconscious import persist
    from app.assistant.subconscious.concern_store import load_register
    day = plan["day"]
    if day < CUTOVER_DAY:
        return {"raised": 0, "updated": 0, "withdrawn": 0}
    lines = lines_ahead(plan, now_utc)
    concerns = day_concerns(load_register(connect=connect), day)
    answer = placements(build_payload(plan, lines, concerns, context), call=call or _agent_call) if lines else []
    counts = persist.apply_calendar_concerns(day, desired(day, answer, lines, concerns, now_utc), now_utc,
                                             connect=connect)
    if any(counts.values()):
        logger.info("[calendar_concerns] %s: %s", day, counts)
    return counts
