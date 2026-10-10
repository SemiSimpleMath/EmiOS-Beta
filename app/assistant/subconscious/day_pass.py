"""The brain's day pass: today's plan, beliefs placed against the calendar (2026-10-01).

Moved from the dayflow pipeline's routine stage into the brain (docs/design/brain_owns_the_day_2026-10-01.md,
step 1): one place reasons about the day. The beliefs it places are the ones the owner approved for the calendar
(the morning review, /beliefs/review; `calendar = 'approved'` in the export); dayflow_routine_writer judges each
one, the reason first, then whether it applies today and the times of day it applies ("all day" for one tied to no
time), given computed facts about the day (`_day_facts`: day of the month, business day). Code builds the day from those
placements: a deterministic hour grid from today's calendar (one slot per local hour, each calendar item in
the hour it starts; long umbrella blocks such as "Work Hours" are backdrops, not slots), each placed belief
in the slot of its time, in its own words. Stored as data (subconscious/day_plan.py), rendered by every
reader. The model never writes the day; it places beliefs (owner, 2026-10-02: "write to db the id, time,
prose ... build the calendar"). Until then the writer wrote guidance per slot, and in about one call in ten
filed a 9 PM item under 9 AM; placements of the same inputs did not, in 100 replays.

Since 2026-10-09 each placement also carries what the belief asks for today (`outcome`) and, for a clock time, until
when it matters (`until`), and becomes a concern (subconscious/calendar_concerns.py; docs/design/concerns_as_state_
2026-10-09.md, rule 7).

Each approved belief is read with the beliefs nearest to it in meaning (`_related`: up to RELATED_MAX, at least
RELATED_MIN cosine over the stored statement embeddings), found by code: the holiday rule beside trash night,
the morning setpoint beside the night one. The writer reads them as context and places only approved beliefs;
the plan line keeps them, and each placement is handed to dayflow from the calendar
(subconscious/belief_occurrences.py). Until 2026-10-03 a selector chose each morning from the whole catalog
(dayflow_belief_selector) and an agent named each selected belief's modifiers (dayflow_belief_modifiers); the
selection differed day to day and placed context as items. Beliefs are not
made into concerns (owner, 2026-10-03); until then each placed belief got an owner concern through the
concern door, and the door merged beliefs about different acts.

It runs on every brain wake (subconscious/brain_wake.py) and is free unless something changed: a plan
is written when today has none, or when its inputs changed (calendar, daily context, the approved beliefs
and their related ones) and the last attempt is at least an hour old.

Inputs: resources/dayflow_pipeline_outputs/resource_daily_context_generator_output.json and
resource_expected_calendar.json (the dayflow pipeline still produces them),
resources/kg_derived/resource_user_beliefs.json (which beliefs are approved), and the intake store's statement
embeddings (for `_related`).
"""
from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_INPUTS_DIR = _get_resources_dir() / "dayflow_pipeline_outputs"
_BELIEFS_PATH = _get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"

_MIN_REGEN_INTERVAL_SECONDS = 3600
_BACKDROP_MIN_HOURS = 4  # calendar items longer than this are backdrops, not slots

BATCH = 10           # approved beliefs per writer call; on 2026-10-05 a call judging all 45 left one out in 2 of 2
                     # live answers (and in 5 of 30 replays), each time a different one
RELATED_MAX = 5      # nearest beliefs shown beside each approved one
RELATED_MIN = 0.65   # least cosine similarity; measured 2026-10-03: trash night drew its holiday rule, pickup
                     # day and timing beliefs and nothing else, the night AC its morning setpoint and the
                     # bedtime temperature check, the monthly timesheets the other timesheet beliefs

_last_attempt_utc: Optional[datetime] = None  # an attempt that failed is not retried within the hour


def _read_json(path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _read_daily_context() -> Dict[str, Any]:
    """The daily context, with the expected calendar's schedule as the authoritative schedule."""
    data = dict(_read_json(_INPUTS_DIR / "resource_daily_context_generator_output.json") or {})
    calendar = _read_json(_INPUTS_DIR / "resource_expected_calendar.json")
    if calendar is not None and isinstance(calendar.get("expected_schedule"), list):
        data["expected_schedule"] = calendar["expected_schedule"]
    return data


def _is_backdrop(item: dict) -> bool:
    """Long umbrella blocks like 'Work Hours' (7.5h) are context, not actionable slots."""
    start, end = parse_iso_utc(item.get("start_utc")), parse_iso_utc(item.get("end_utc"))
    if start is None or end is None:
        return False
    return (end - start) >= timedelta(hours=_BACKDROP_MIN_HOURS)


def _day_items(ctx_data: Dict[str, Any], now_utc: datetime) -> Tuple[List[dict], List[dict]]:
    """Calendar items still relevant (ended no earlier than an hour ago): (slot items, backdrops)."""
    cutoff = now_utc - timedelta(hours=1)
    specific, backdrops = [], []
    for item in ctx_data.get("expected_schedule") or []:
        if not isinstance(item, dict):
            continue
        start, end = parse_iso_utc(item.get("start_utc")), parse_iso_utc(item.get("end_utc"))
        if start is None or end is None or end <= cutoff:
            continue
        (backdrops if _is_backdrop(item) else specific).append(item)
    return specific, sorted(backdrops, key=lambda x: str(x.get("start_utc") or ""))


def _item_line(item: dict) -> str:
    start, end, status = item.get("start_local", ""), item.get("end_local", ""), item.get("status", "")
    when = f"{start} - {end}" if end else start
    return f"  {item.get('title', '')} {when}" + (f" ({status})" if status else "")


def _format_daily_context(ctx_data: Dict[str, Any], items: List[dict], backdrops: List[dict]) -> str:
    """The day's facts: backdrops, schedule, current status, milestones. Not the tracker's day theme: it is a
    summary, and until 2026-10-05 the tracker read this plan, so a placement came back as a stated fact ("monthly
    timesheets are due by 5 PM on this first business day of the month", on the 3rd business day) and the writer
    cited it over the date facts."""
    if not ctx_data:
        return "(no daily context available yet)"
    parts: List[str] = []
    if backdrops:
        parts.append("\n".join(["Today's backdrops (long blocks that frame the day):"]
                                + [_item_line(item) for item in backdrops]))
    ordered = sorted(items, key=lambda it: str(it.get("start_utc") or ""))
    parts.append("\n".join(["Today's schedule:"] + ([_item_line(item) for item in ordered] or ["  (nothing scheduled)"])))
    if ctx_data.get("current_status"):
        parts.append(f"Current status: {ctx_data['current_status']}")
    milestones = ctx_data.get("milestones") or []
    if milestones:
        lines = ["Milestones (what has already happened):"]
        for m in milestones:
            flag = " [ongoing]" if m.get("ongoing") else ""
            lines.append(f"  {m.get('time', '?')} — {m.get('description', '')}{flag}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _read_belief_entries() -> list[dict]:
    if not _BELIEFS_PATH.exists():
        raise FileNotFoundError(f"Belief export missing: {_BELIEFS_PATH}")
    data = json.loads(_BELIEFS_PATH.read_text(encoding="utf-8"))
    return [e for e in data.get("beliefs", [])
            if e.get("statement") and e.get("status", "active") == "active"]


def _render_belief_block(entries: list) -> str:
    """Beliefs as the writer reads them, each with its related beliefs; whether one applies today is the writer's
    decision."""
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    beliefs = sorted((e for e in entries if e.get("statement") and e.get("status", "active") == "active"),
                     key=lambda e: str(e.get("belief_key", "")))
    return _jinja_env.get_template("dayflow_routine_writer/prompts/beliefs.j2").render(beliefs=beliefs)


def _belief_vectors() -> Dict[str, List[float]]:
    """belief_key -> the stored statement embedding, for every active belief."""
    from belief_engine.intake.run import app_store
    return {b["id"]: b["embedding"] for b in app_store().beliefs()}


def _related(pool: List[dict], entries: List[dict], vectors: Dict[str, List[float]]) -> Dict[str, List[Dict[str, str]]]:
    """For each approved belief, the active beliefs nearest to it in meaning (belief_key -> [{belief_key,
    statement}], nearest first): up to RELATED_MAX, each at least RELATED_MIN cosine."""
    import math
    statements = {e["belief_key"]: e["statement"] for e in entries}

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))

    out = {}
    for e in pool:
        key = e["belief_key"]
        v = vectors[key]
        near = sorted(((cos(v, vectors[k]), k) for k in statements if k != key and k in vectors), reverse=True)
        out[key] = [{"belief_key": k, "statement": statements[k]} for sim, k in near[:RELATED_MAX] if sim >= RELATED_MIN]
    return out


def _routine_inputs_fingerprint(boundary_date_local: str, expected_schedule: list, beliefs_block: str,
                                *, daily_context: Optional[dict] = None) -> str:
    """The exact decision inputs, excluding wall-clock/scaffold rendering and generated timestamps. The beliefs
    block is the approved beliefs with their related ones, as the writer reads them."""
    context = daily_context or {}
    payload = {"projection_version": 7, "day": boundary_date_local,
               "schedule": expected_schedule, "beliefs": beliefs_block,
               "current_status": context.get("current_status"), "milestones": context.get("milestones")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _day_facts(day: str) -> Dict[str, Any]:
    """The planned day's place in its month, computed, so a belief tied to a day of the month or a business day is
    judged against facts rather than the writer's arithmetic (2026-10-05: on Monday the 5th the monthly timesheets
    "first business day of the month" were placed in 6 of 9 runs). Business days are Monday to Friday; holidays are
    not known here."""
    d = date.fromisoformat(day)
    last = calendar.monthrange(d.year, d.month)[1]
    days = [date(d.year, d.month, n) for n in range(1, last + 1)]
    business = [x for x in days if x.weekday() < 5]
    same_weekday = [x for x in days if x.weekday() == d.weekday()]

    def text(x: date) -> str:
        return f"{x:%A}, {x:%B} {x.day}"
    return {"weekday": f"{d:%A}", "date": f"{d:%B} {d.day}, {d.year}", "iso": day, "month": f"{d:%B}",
            "day_of_month": d.day, "days_in_month": last,
            "weekday_number": same_weekday.index(d) + 1, "weekday_is_last": same_weekday[-1] == d,
            "business_day_number": business.index(d) + 1 if d in business else None,
            "business_days_in_month": len(business),
            "first_business_day": text(business[0]), "last_business_day": text(business[-1])}


def _write(agent_input: Dict[str, Any]) -> Dict[str, Any]:
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.utils.pydantic_classes import Message
    agent = DI.agent_factory.create_agent("dayflow_routine_writer")
    if agent is None:
        raise RuntimeError("dayflow_routine_writer agent not found")
    scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="dayflow_routine_runner")
    data = getattr(agent.action_handler(Message(scope_context=scope, agent_input=agent_input)), "data", None)
    if not isinstance(data, dict):
        raise RuntimeError("dayflow_routine_writer returned no structured result")
    return data


def _clock_hour_minute(text: str) -> Optional[Tuple[int, int]]:
    """A clock time as the writer writes it ("9:00 PM", "7:30 am") as (hour, minute); None if unreadable."""
    try:
        t = datetime.strptime(" ".join(str(text).upper().split()), "%I:%M %p")
    except ValueError:
        return None
    return t.hour, t.minute


def _problems(data: Dict[str, Any], belief_keys: set, conditions: set = frozenset()) -> List[str]:
    """What is wrong with the writer's judgments: a key that is not an approved belief (a related belief is read
    beside the one it relates to and is never an entry of its own: on 2026-10-03 the condition "keeps the
    bedroom fan running continuously" was placed for all day and handed over as work), an approved belief judged
    twice or not at all, a belief that applies with no outcome or no time, a time that is neither a clock time nor
    'all day', or a clock time without a clock time it matters until."""
    out = []
    judged = [str(j.get("belief_key") or "") for j in data.get("judgments") or []]
    for j in data.get("judgments") or []:
        key = str(j.get("belief_key") or "")
        if key not in belief_keys:
            bare = key.removeprefix("belief:")
            out.append(f"{key!r}: write the key alone, as {bare!r}" if bare in belief_keys
                       else f"{key!r} is read beside the approved beliefs, not an entry: leave it out"
                       if key in conditions else f"{key!r} is not one of the beliefs given")
        if not j.get("applies_today"):
            continue
        if not str(j.get("outcome") or "").strip():
            out.append(f"{key}: it applies today, so say what it asks for today, as an outcome")
        if not j.get("times"):
            out.append(f"{key}: it applies today, so give the time it applies, or 'all day'")
        for p in j.get("times") or []:
            when, until = str((p or {}).get("time") or "").strip(), str((p or {}).get("until") or "").strip()
            if when.lower() == "all day":
                continue
            if _clock_hour_minute(when) is None:
                out.append(f"{key}: time {when!r} is not a clock time like '9:00 PM', or 'all day'")
            elif _clock_hour_minute(until) is None:
                out.append(f"{key}: at {when}, until {until!r} is not a clock time like '11:00 PM'")
    for key in sorted(belief_keys):
        if judged.count(key) != 1:
            out.append(f"{key}: judge each approved belief once" if judged.count(key) else
                       f"{key}: no judgment; judge every approved belief")
    return out


def _placements(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """The beliefs judged to apply today, one placement per time. Until 2026-10-05 the writer returned placements
    alone and listed beliefs its own reason said did not apply (the monthly timesheets on the 3rd business day,
    "business day 3, so this does not apply", placed all day): 13 of 30 replays; judged reason first, 6 of 30, and
    1 of 30 without the day theme."""
    return [{"belief_key": j["belief_key"], "time": p["time"], "until": p.get("until") or "",
             "outcome": str(j.get("outcome") or "").strip()}
            for j in data.get("judgments") or [] if j.get("applies_today") for p in j.get("times") or []]


def _place(placements: List[Dict[str, Any]], slots: List[dict], statements: Dict[str, str],
           related: Optional[Dict[str, List[Dict[str, str]]]] = None) -> Tuple[List[dict], List[dict]]:
    """Each placement in the slot of its hour, in time order, as data (time, belief key, statement, and the
    belief's related beliefs); 'all day' ones are the day's flags. A time whose hour is not in the grid (before its
    first hour) has passed and is not stored. When the grid spans midnight an hour label repeats; the later
    slot is today's."""
    related = related or {}
    slot_of_hour = {datetime.fromisoformat(s["hour_local_iso"]).hour: s["slot"] for s in slots}
    timed: Dict[str, List[Tuple[int, dict]]] = {s["slot"]: [] for s in slots}
    flags: List[dict] = []
    seen = set()
    for p in placements:
        key, when = p["belief_key"], " ".join(str(p["time"]).split())
        if (key, when.lower()) in seen:
            continue
        seen.add((key, when.lower()))
        line = {"time": "all day" if when.lower() == "all day" else when, "belief_key": key,
                "statement": statements[key], "related": related.get(key, []), "outcome": p["outcome"],
                "until": "" if when.lower() == "all day" else " ".join(str(p["until"]).split())}
        if when.lower() == "all day":
            flags.append(line)
            continue
        hour, minute = _clock_hour_minute(when)
        if hour in slot_of_hour:
            timed[slot_of_hour[hour]].append((minute, line))
    return ([{"slot": s["slot"], "hour_local_iso": s["hour_local_iso"], "items": s["items"],
              "lines": [line for _, line in sorted(timed[s["slot"]], key=lambda t: t[0])]} for s in slots], flags)


def _judge(batch: List[dict], related: Dict[str, List[Dict[str, str]]], base: Dict[str, Any], conditions: set,
           step) -> List[Dict[str, Any]]:
    """One writer call for a batch of approved beliefs (each with its related ones), against the day's facts and
    schedule; one correction, then a loud failure."""
    keys = {e["belief_key"] for e in batch}
    agent_input = {**base, "beliefs_block": _render_belief_block(
        [{**e, "related": related[e["belief_key"]]} for e in batch])}
    data = _write(agent_input)
    problems = _problems(data, keys, conditions)
    if problems:
        logger.warning("[day_pass] invalid judgments, one correction: %s", problems)
        step("day_pass", "answer refused, one correction", problems=problems)
        data = _write({**agent_input, "correction": "; ".join(problems)})
        problems = _problems(data, keys, conditions)
        if problems:
            raise ValueError(f"routine writer judgments still invalid after one correction: {problems}")
    return data.get("judgments") or []


def run_day_pass(now_utc: Optional[datetime] = None, *, connect=None, concern_connect=None) -> Dict[str, Any]:
    """Write today's plan when it is missing or its inputs changed (at most hourly), and match the day's calendar
    concerns to it. `connect` is the plan store and `concern_connect` the concern register: test seams."""
    global _last_attempt_utc
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import build_hour_grid, end_of_local_day_utc
    from app.assistant.subconscious import day_plan

    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(get_local_timezone())
    day = boundary_date_local_str(now_local)

    ctx_data = _read_daily_context()
    entries = _read_belief_entries()
    pool = [e for e in entries if e.get("calendar") == "approved"]
    related = _related(pool, entries, _belief_vectors())
    beliefs_block = _render_belief_block([{**e, "related": related[e["belief_key"]]} for e in pool])
    fingerprint = _routine_inputs_fingerprint(day, ctx_data.get("expected_schedule") or [], beliefs_block,
                                              daily_context=ctx_data)
    plan = day_plan.load_plan(day, connect=connect)
    if plan is not None:
        if plan["inputs_fingerprint"] == fingerprint:
            return {"day_pass": "unchanged"}
        last = max(t for t in (parse_iso_utc(plan["generated_at_utc"]), _last_attempt_utc) if t is not None)
        if (now_utc - last).total_seconds() < _MIN_REGEN_INTERVAL_SECONDS:
            return {"day_pass": "changed_too_soon"}

    _last_attempt_utc = now_utc
    specific, backdrops = _day_items(ctx_data, now_utc)
    slots = build_hour_grid(specific, now_utc, end_of_local_day_utc(now_utc))
    daily_context_block = _format_daily_context(ctx_data, specific, backdrops)
    given = {e["belief_key"]: e["statement"] for e in pool}
    conditions = {r["belief_key"] for found in related.values() for r in found} - set(given)
    # The day is the planned day (the boundary day); the clock is shown apart, so before the boundary hour the
    # header does not name the next calendar day's weekday beside this day's date (until 2026-10-05 it did).
    base = {
        "day_facts": _day_facts(day), "now_local": f"{now_local:%A, %B} {now_local.day}, {now_local.strftime('%I:%M %p').lstrip('0')}",
        "daily_context": daily_context_block}
    from app.assistant.subconscious.brain_trace import step
    ordered = sorted(pool, key=lambda e: str(e["belief_key"]))
    judgments: List[Dict[str, Any]] = []
    for i in range(0, len(ordered), BATCH):
        batch = ordered[i:i + BATCH]
        judgments += _judge(batch, related, base, conditions, step)
    data = {"judgments": judgments}
    placements = _placements(data)
    plan_slots, flags = _place(placements, slots, given, related)
    step("day_pass", "placed", day=day, approved=sorted(given),
         related={k: [r["belief_key"] for r in v] for k, v in related.items()},
         placements=placements, judgments=data.get("judgments") or [])
    plan = {
        "day": day, "generated_at_utc": now_utc.isoformat(), "inputs_fingerprint": fingerprint,
        "selected_belief_keys": sorted(given),                # the beliefs approved for the calendar
        "qualifier_belief_keys": sorted(conditions),         # the related beliefs read beside them
        "selection_reason": "the beliefs the owner approved for the calendar", "change_summary": "",
        "backdrops": backdrops, "flags": flags, "slots": plan_slots}
    day_plan.save_plan(plan, connect=connect)
    step("day_pass", "written", day=day)
    # Each placement is a concern (rule 7): raised, updated or withdrawn to match the plan.
    from app.assistant.subconscious import calendar_concerns
    concerns = calendar_concerns.apply(plan, now_utc, connect=concern_connect)
    step("day_pass", "concerns", day=day, **concerns)
    logger.info("[day_pass] wrote the plan for %s: %d placements of %d approved beliefs (%d related)",
                day, sum(len(s["lines"]) for s in plan_slots) + len(flags), len(given), len(conditions))
    return {"day_pass": "written", "day": day, "slots": len(plan_slots), "calendar_concerns": concerns}
