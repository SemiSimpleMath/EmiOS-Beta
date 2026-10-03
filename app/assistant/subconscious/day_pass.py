"""The brain's day pass: today's plan, beliefs placed against the calendar (2026-10-01).

Moved from the dayflow pipeline's routine stage into the brain (docs/design/brain_owns_the_day_2026-10-01.md,
step 1): one place reasons about the day. dayflow_belief_selector chooses the beliefs that bear on today,
reading the catalog in whole topics; dayflow_routine_writer says, for each that applies today, the time of
day it applies (a belief once per time; "all day" for one tied to no time). Code builds the day from those
placements: a deterministic hour grid from today's calendar (one slot per local hour, each calendar item in
the hour it starts; long umbrella blocks such as "Work Hours" are backdrops, not slots), each placed belief
in the slot of its time, in its own words. Stored as data (subconscious/day_plan.py), rendered by every
reader. The model never writes the day; it places beliefs (owner, 2026-10-02: "write to db the id, time,
prose ... build the calendar"). Until then the writer wrote guidance per slot, and in about one call in ten
filed a 9 PM item under 9 AM; placements of the same inputs did not, in 100 replays.

Every placed belief has an owner: a concern (docs/design/day_items_have_owners_2026-10-02.md). A belief an
open concern already holds (`belief_refs`) is owned by it, found by code; the rest go through the concern
door together as standing candidates (a recurring part of the day), merged by habit or into an existing
concern, or admitted new. Today's times are recorded on each owner (`occurrences`), from which the brain
hands them to dayflow (subconscious/concern_handoff.py). Each plan line names its owner.

It runs on every brain wake (subconscious/brain_wake.py) and is free unless something changed: a plan
is written when today has none, or when its inputs changed (calendar, daily context, belief catalog,
weekly insights) and the last attempt is at least an hour old.

Inputs: resources/dayflow_pipeline_outputs/resource_daily_context_generator_output.json and
resource_expected_calendar.json (the dayflow pipeline still produces them),
resources/kg_derived/resource_user_beliefs.json, and the weekly insights (for the selector).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_INPUTS_DIR = _get_resources_dir() / "dayflow_pipeline_outputs"
_BELIEFS_PATH = _get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"
_WEEKLY_INSIGHTS_PATH = _get_resources_dir() / "weekly_insights_pipeline_outputs" / "resource_weekly_insights_latest.json"

_MIN_REGEN_INTERVAL_SECONDS = 3600
_BACKDROP_MIN_HOURS = 4  # calendar items longer than this are backdrops, not slots

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
    if not ctx_data:
        return "(no daily context available yet)"
    parts: List[str] = []
    if ctx_data.get("day_theme"):
        parts.append(f"Day theme: {ctx_data['day_theme']}")
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
    """Preserve eligible beliefs and their qualifiers; relevance is an LLM decision."""
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    beliefs = sorted((e for e in entries if e.get("statement") and e.get("status", "active") == "active"),
                     key=lambda e: str(e.get("belief_key", "")))
    return _jinja_env.get_template("dayflow_routine_writer/prompts/beliefs.j2").render(beliefs=beliefs)


def _by_topic(entries: list[dict]) -> list[list[dict]]:
    """Entries grouped by topic (belief_engine/clusters.py), topics in label order; a belief not yet
    placed is a topic of its own."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e.get("cluster_id") or f"~{e['belief_key']}", []).append(e)
    return [groups[k] for k in sorted(groups, key=lambda k: (groups[k][0].get("cluster") or "~", k))]


def _topic_pages(entries: list[dict], max_chars: int) -> list[list[dict]]:
    """Whole topics per page: a belief is always judged with the beliefs that qualify it."""
    from belief_engine.matching.context import encode
    pages, page, size = [], [], 0
    for group in _by_topic(entries):
        length = sum(len(encode(e)) for e in group)
        if page and size + length > max_chars:
            pages.append(page)
            page, size = [], 0
        page.extend(group)
        size += length
    if page:
        pages.append(page)
    return pages


def _select_beliefs(entries: list[dict], daily_context: str, scope, *,
                    weekly_insights: str = "") -> tuple[list[dict], str, list[dict]]:
    """The agent selects by belief_key, the one id each record shows; Python restores exact original
    records. The catalog goes in pages of whole topics (owner, 2026-09-30: relevant beliefs cluster).
    Returns the beliefs that change what the assistant does today, the reasoning, and the beliefs the
    selector named as conditions on them (their day, limit, exception, reminder wish), for the writer."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    from app.assistant.agents.dayflow_belief_selector.agent_form import AgentForm
    by_key = {e["belief_key"]: e for e in entries}
    if len(by_key) != len(entries):
        raise ValueError("Duplicate belief keys in export")
    if not entries:
        return [], "Empty active belief catalog", []
    from belief_engine.matching.selection import select_records

    def invoke(records, retry):
        agent = DI.agent_factory.create_agent("dayflow_belief_selector")
        if agent is None:
            raise RuntimeError("dayflow_belief_selector agent unavailable")
        # One id per record: short_id repeats the belief_key and cluster_id is a second id; the topic
        # name (`cluster`) carries the grouping.
        shown = [{k: v for k, v in r.items() if k not in ("short_id", "cluster_id")} for r in records]
        result = agent.action_handler(Message(scope_context=scope, agent_input={
            "daily_context": daily_context, "belief_catalog": shown,
            "weekly_insights": weekly_insights, "selection_retry": retry}))
        return getattr(result, "data", None)

    selected, reasons, qualifiers = [], [], []
    for page in _topic_pages(entries, max_chars=48000):
        chosen, reason, extra = select_records(page, invoke, AgentForm, extra=("qualifier_keys",))
        selected.extend(chosen)
        qualifiers.extend(extra["qualifier_keys"])
        reasons.append(reason)
    chosen_keys = {e["belief_key"] for e in selected}
    qualifiers = list({e["belief_key"]: e for e in qualifiers if e["belief_key"] not in chosen_keys}.values())
    return selected, "\n".join(reasons), qualifiers


def _format_weekly_insights() -> str:
    data = _read_json(_WEEKLY_INSIGHTS_PATH)
    if data is None:
        return ""
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    return _jinja_env.get_template("dayflow_routine_writer/prompts/weekly.j2").render(weekly=data)


def _routine_inputs_fingerprint(boundary_date_local: str, expected_schedule: list, beliefs_block: str,
                                *, daily_context: Optional[dict] = None, weekly_insights: str = "",
                                belief_catalog: Optional[list] = None) -> str:
    """The exact decision inputs, excluding wall-clock/scaffold rendering and generated timestamps."""
    context = daily_context or {}
    payload = {"projection_version": 4, "day": boundary_date_local,
               "schedule": expected_schedule, "beliefs": beliefs_block, "belief_catalog": belief_catalog,
               "day_theme": context.get("day_theme"), "current_status": context.get("current_status"),
               "milestones": context.get("milestones"), "weekly_insights": weekly_insights}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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


def _problems(data: Dict[str, Any], belief_keys: set) -> List[str]:
    """What is wrong with the writer's placements: an unknown belief key, or a time that is neither a clock
    time nor 'all day'."""
    out = []
    for p in data.get("placements") or []:
        key, when = str(p.get("belief_key") or ""), str(p.get("time") or "")
        if key not in belief_keys:
            bare = key.removeprefix("belief:")
            out.append(f"{key!r}: write the key alone, as {bare!r}" if bare in belief_keys
                       else f"{key!r} is not one of the beliefs given")
        if when.strip().lower() != "all day" and _clock_hour_minute(when) is None:
            out.append(f"{key}: time {when!r} is not a clock time like '9:00 PM', or 'all day'")
    return out


def _place(placements: List[Dict[str, Any]], slots: List[dict],
           statements: Dict[str, str]) -> Tuple[List[dict], List[dict]]:
    """Each placement in the slot of its hour, in time order, as data (time, belief key, statement); 'all day'
    ones are the day's flags. A time whose hour is not in the grid (before its first hour) has passed and is
    not stored. When the grid spans midnight an hour label repeats; the later slot is today's."""
    slot_of_hour = {datetime.fromisoformat(s["hour_local_iso"]).hour: s["slot"] for s in slots}
    timed: Dict[str, List[Tuple[int, dict]]] = {s["slot"]: [] for s in slots}
    flags: List[dict] = []
    seen = set()
    for p in placements:
        key, when = p["belief_key"], " ".join(str(p["time"]).split())
        if (key, when.lower()) in seen:
            continue
        seen.add((key, when.lower()))
        if when.lower() == "all day":
            flags.append({"time": "all day", "belief_key": key, "statement": statements[key]})
            continue
        hour, minute = _clock_hour_minute(when)
        if hour in slot_of_hour:
            timed[slot_of_hour[hour]].append((minute, {"time": when, "belief_key": key, "statement": statements[key]}))
    return ([{"slot": s["slot"], "hour_local_iso": s["hour_local_iso"], "items": s["items"],
              "lines": [line for _, line in sorted(timed[s["slot"]], key=lambda t: t[0])]} for s in slots], flags)


def _candidate(key: str, statement: str, now_iso: str) -> Dict[str, Any]:
    """A standing concern candidate for one placed belief nothing owns yet."""
    return {"label": key, "title": statement, "subject": "household", "kind": "standing",
            "severity": "low", "horizon": "long_horizon",
            "done_when": f"Each day it applies, handled at its time: {statement}",
            "notes": "", "domain_tags": [], "addressable_by": [], "first_observed": now_iso,
            "evidence": [{"kind": "belief", "ref": f"belief:{key}", "snippet": statement}],
            "belief_refs": [key]}


def _assign_owners(day: str, plan_slots: List[dict], flags: List[dict], statements: Dict[str, str],
                   now_utc: datetime, *, register_connect=None, judge=None) -> None:
    """Every placed belief's owner, set on its plan line as `concern_id` (None when a closed concern
    settled it). Beliefs no concern holds go through the concern door in one call; today's times are
    recorded on the owners."""
    from app.assistant.subconscious import concern_door, persist
    from app.assistant.subconscious.concern_store import load_register
    lines = [line for s in plan_slots for line in s["lines"]] + flags
    occurrences: Dict[str, List[Dict[str, Any]]] = {}
    for line in lines:
        occurrences.setdefault(line["belief_key"], []).append({"time": line["time"]})
    register = load_register(connect=register_connect)
    held = {k for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []
            for k in c.get("belief_refs") or []}
    now_iso = now_utc.isoformat()
    candidates = [_candidate(k, statements[k], now_iso) for k in occurrences if k not in held]
    plan = concern_door.plan_admission(candidates, register, judge=judge) if candidates else []
    owners = persist.record_day_beliefs(plan, occurrences, day, connect=register_connect)
    for line in lines:
        line["concern_id"] = owners.get(line["belief_key"])


def run_day_pass(now_utc: Optional[datetime] = None, *, connect=None, register_connect=None,
                 judge=None) -> Dict[str, Any]:
    """Write today's plan when it is missing or its inputs changed (at most hourly). `connect` is the
    plan store, `register_connect` the concerns register, `judge` the concern door: test seams."""
    global _last_attempt_utc
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import build_hour_grid, end_of_local_day_utc
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.subconscious import day_plan

    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(get_local_timezone())
    day = boundary_date_local_str(now_local)

    ctx_data = _read_daily_context()
    entries = _read_belief_entries()
    beliefs_block = _render_belief_block(entries)
    weekly = _format_weekly_insights()
    fingerprint = _routine_inputs_fingerprint(day, ctx_data.get("expected_schedule") or [], beliefs_block,
                                              daily_context=ctx_data, weekly_insights=weekly, belief_catalog=entries)
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
    scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="dayflow_belief_selector")
    selected, selection_reason, qualifiers = _select_beliefs(entries, daily_context_block, scope,
                                                             weekly_insights=weekly)
    given = {e["belief_key"]: e["statement"] for e in [*selected, *qualifiers]}
    agent_input = {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M"), "day_of_week": now_local.strftime("%A"),
        "boundary_date_local": day, "daily_context": daily_context_block,
        "beliefs_block": _render_belief_block(selected), "related_beliefs_block": _render_belief_block(qualifiers)}
    data = _write(agent_input)
    problems = _problems(data, set(given))
    if problems:
        logger.warning("[day_pass] invalid placements, one correction: %s", problems)
        data = _write({**agent_input, "correction": "; ".join(problems)})
        problems = _problems(data, set(given))
        if problems:
            raise ValueError(f"routine writer placements still invalid after one correction: {problems}")
    plan_slots, flags = _place(data.get("placements") or [], slots, given)
    _assign_owners(day, plan_slots, flags, given, now_utc, register_connect=register_connect, judge=judge)
    day_plan.save_plan({
        "day": day, "generated_at_utc": now_utc.isoformat(), "inputs_fingerprint": fingerprint,
        "selected_belief_keys": [e["belief_key"] for e in selected],
        "qualifier_belief_keys": [e["belief_key"] for e in qualifiers],
        "selection_reason": selection_reason, "change_summary": "",
        "backdrops": backdrops, "flags": flags, "slots": plan_slots}, connect=connect)
    logger.info("[day_pass] wrote the plan for %s: %d placements of %d beliefs (+%d qualifiers)",
                day, sum(len(s["lines"]) for s in plan_slots) + len(flags), len(selected), len(qualifiers))
    return {"day_pass": "written", "day": day, "slots": len(plan_slots)}
