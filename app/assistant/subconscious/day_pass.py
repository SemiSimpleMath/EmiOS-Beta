"""The brain's day pass: today's plan, beliefs placed against the calendar (2026-10-01).

Moved from the dayflow pipeline's routine stage into the brain (docs/design/brain_owns_the_day_2026-10-01.md,
step 1): one place reasons about the day. It builds a deterministic hour grid from today's calendar (one
slot per local hour, each calendar item in the hour it starts; long umbrella blocks such as "Work Hours"
are backdrops, not slots), lets dayflow_belief_selector choose the beliefs that bear on today, reading the
catalog in whole topics, and lets dayflow_routine_writer write guidance per slot. The result is stored as
data (subconscious/day_plan.py) and rendered fresh by every reader.

It runs on every brain wake (subconscious/brain_wake.py) and is free unless something changed: a plan
is written when today has none, or when its inputs changed (calendar, daily context, belief catalog,
weekly insights) and the last attempt is at least an hour old.

Inputs: resources/dayflow_pipeline_outputs/resource_daily_context_generator_output.json and
resource_expected_calendar.json (the dayflow pipeline still produces them),
resources/kg_derived/resource_user_beliefs.json, the weekly insights, and the open concerns (step 1b): a
matter an open concern owns appears in the plan as a pointer to that concern, never as its own reminder.
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


def _format_daily_context(ctx_data: Dict[str, Any], slots: List[dict], backdrops: List[dict]) -> str:
    from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import render_hour_grid
    if not ctx_data:
        return "(no daily context available yet)"
    parts: List[str] = []
    if ctx_data.get("day_theme"):
        parts.append(f"Day theme: {ctx_data['day_theme']}")
    if backdrops:
        lines = ["Today's backdrops (umbrella blocks — context, not slots to fill):"]
        for item in backdrops:
            start, end = item.get("start_local", ""), item.get("end_local", "")
            lines.append(f"  {item.get('title', '')} {f'{start} - {end}' if end else start}")
        parts.append("\n".join(lines))
    parts.append(render_hour_grid(slots))
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


def _open_concerns() -> List[Dict[str, Any]]:
    """The concerns the brain is tracking (active and addressing), as the writer reads them: what each
    is, what settles it, the brief's readiness, and the work attached to it that is still in progress."""
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            brief = c.get("brief") or {}
            readiness = brief.get("readiness") or {}
            out.append({"concern_id": c["concern_id"], "title": c.get("title") or "", "status": bucket,
                        "done_when": c.get("done_when") or "", "what": brief.get("what") or "",
                        "readiness": readiness.get("decision") or "", "hold_until": readiness.get("hold_until") or "",
                        "work_in_progress": [w.get("objective") or w.get("title") or w_id
                                             for w_id, w in (c.get("attached_work") or {}).items()
                                             if w.get("status") == "active"]})
    return out


def _render_concern_block(concerns: List[Dict[str, Any]]) -> str:
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    return _jinja_env.get_template("dayflow_routine_writer/prompts/concerns.j2").render(concerns=concerns)


def _format_weekly_insights() -> str:
    data = _read_json(_WEEKLY_INSIGHTS_PATH)
    if data is None:
        return ""
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    return _jinja_env.get_template("dayflow_routine_writer/prompts/weekly.j2").render(weekly=data)


def _routine_inputs_fingerprint(boundary_date_local: str, expected_schedule: list, beliefs_block: str,
                                *, daily_context: Optional[dict] = None, weekly_insights: str = "",
                                belief_catalog: Optional[list] = None, concerns: Optional[list] = None) -> str:
    """The exact decision inputs, excluding wall-clock/scaffold rendering and generated timestamps."""
    context = daily_context or {}
    payload = {"projection_version": 3, "day": boundary_date_local,
               "schedule": expected_schedule, "beliefs": beliefs_block, "belief_catalog": belief_catalog,
               "day_theme": context.get("day_theme"), "current_status": context.get("current_status"),
               "milestones": context.get("milestones"), "weekly_insights": weekly_insights,
               "concerns": concerns}
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


def _validated(data: Dict[str, Any], slots: List[dict], belief_keys: set,
               concern_ids: set = frozenset()) -> Tuple[List[dict], List[dict]]:
    """The writer's slots in grid order with their calendar items, and its flags. Every grid slot exactly
    once and only grid slots; every cited belief key and concern id one the writer was given."""
    returned = [s["slot"] for s in data.get("slots") or []]
    grid = [s["slot"] for s in slots]
    if sorted(returned) != sorted(grid):
        raise ValueError(f"routine writer slots {returned} are not the grid's slots {grid}")
    lines_by_slot = {s["slot"]: s["lines"] for s in data["slots"]}
    flags = list(data.get("flags") or [])
    cited = {k for lines in [*lines_by_slot.values(), flags] for line in lines for k in line["belief_keys"]}
    unknown = sorted(cited - belief_keys)
    if unknown:
        raise ValueError(f"routine writer cited belief keys it was not given: {unknown}")
    pointed = {c for lines in [*lines_by_slot.values(), flags] for line in lines for c in line["concern_ids"]}
    unknown = sorted(pointed - set(concern_ids))
    if unknown:
        raise ValueError(f"routine writer pointed to concerns it was not given: {unknown}")
    return ([{"slot": s["slot"], "hour_local_iso": s["hour_local_iso"], "items": s["items"],
              "lines": lines_by_slot[s["slot"]]} for s in slots], flags)


def run_day_pass(now_utc: Optional[datetime] = None, *, connect=None) -> Dict[str, Any]:
    """Write today's plan when it is missing or its inputs changed (at most hourly)."""
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
    concerns = _open_concerns()
    fingerprint = _routine_inputs_fingerprint(day, ctx_data.get("expected_schedule") or [], beliefs_block,
                                              daily_context=ctx_data, weekly_insights=weekly, belief_catalog=entries,
                                              concerns=concerns)
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
    daily_context_block = _format_daily_context(ctx_data, slots, backdrops)
    scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="dayflow_belief_selector")
    selected, selection_reason, qualifiers = _select_beliefs(entries, daily_context_block, scope,
                                                             weekly_insights=weekly)
    data = _write({
        "date_time": now_local.strftime("%Y-%m-%d %H:%M"), "day_of_week": now_local.strftime("%A"),
        "boundary_date_local": day, "daily_context": daily_context_block,
        "beliefs_block": _render_belief_block(selected), "related_beliefs_block": _render_belief_block(qualifiers),
        "weekly_insights_block": weekly, "concerns_block": _render_concern_block(concerns)})
    plan_slots, flags = _validated(data, slots, {e["belief_key"] for e in [*selected, *qualifiers]},
                                   {c["concern_id"] for c in concerns})
    day_plan.save_plan({
        "day": day, "generated_at_utc": now_utc.isoformat(), "inputs_fingerprint": fingerprint,
        "selected_belief_keys": [e["belief_key"] for e in selected],
        "qualifier_belief_keys": [e["belief_key"] for e in qualifiers],
        "selection_reason": selection_reason, "change_summary": str(data.get("change_summary") or ""),
        "backdrops": backdrops, "flags": flags, "slots": plan_slots}, connect=connect)
    logger.info("[day_pass] wrote the plan for %s: %d slots, %d beliefs (+%d qualifiers) | %s",
                day, len(plan_slots), len(selected), len(qualifiers), data.get("change_summary"))
    return {"day_pass": "written", "day": day, "slots": len(plan_slots)}
