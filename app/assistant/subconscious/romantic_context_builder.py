"""Build the injected context dict for the romantic_proposer agent.

Mirrors wellness_context_builder shape. Pulls relationship signals
from KG (Jukka↔Katy entity card description + recent intention.romantic
pods), key dates (anniversary, birthdays) from resource_user_data,
calendars from the existing calendar tool wrappers.

For v0, several builders are graceful stubs that return useful empty
states when data isn't yet structured — same pattern as the wellness
activity/sleep stubs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from app.assistant.subconscious.context_builder import (
    _build_calendar_today_tomorrow,
    _build_family_roster,
    _resolve_household_members,
    build_weekly_schedule_block,
    _NO_DATA_FMT,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


def build_romantic_proposer_context() -> Dict[str, str]:
    """Context for romantic_proposer (7-14 day horizon, daily cadence)."""
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "relationship_signal_30d": _build_relationship_signal(),
        "food_calendar": _build_food_calendar(now_local=now_local),
        "wellness_calendar": _build_wellness_calendar(now_local=now_local),
        "general_calendar": _build_calendar_today_tomorrow(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "addressable_concerns": _build_addressable_concerns(),
        "recent_romantic_intentions": _build_recent_romantic_intentions(),
        "key_dates": _build_key_dates(now_local=now_local),
        "weekly_schedule": build_weekly_schedule_block(),
    }


# ── section builders ─────────────────────────────────────────────────────


def _build_relationship_signal() -> str:
    """Scrape relationship-relevant mentions from Katy's KG entity card +
    any direct (Jukka, Katy) edge descriptions.

    v0 stub: filter Katy's entity card description for relationship
    keywords. When structured relationship logging lands, this becomes
    richer."""
    try:
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        from app.models.base import get_session

        session = get_session()
        try:
            row = (
                session.query(Node)
                .filter(Node.label == "Katy")
                .filter(Node.node_type == "Entity")
                .one_or_none()
            )
        finally:
            session.close()

        if row is None or not row.description:
            return "(no KG entity card for Katy yet — propose conservatively, defer to key_dates + concerns)"

        keywords = (
            "anniversary", "wedding", "marriage", "date night", "vacation",
            "trip", "weekend away", "birthday", "favorite", "loves",
            "stressed", "happy", "tired", "tension", "argument", "fight",
            "appreciat", "missed", "together", "alone time",
        )
        sentences = (row.description or "").split(". ")
        relevant = [s.strip() for s in sentences if any(k in s.lower() for k in keywords)]
        if not relevant:
            return "(no relationship-keyword mentions in Katy's KG card; propose conservatively)"
        return "Relationship signals from Katy's KG card:\n" + "\n".join(f"- {s}." for s in relevant)
    except Exception as e:
        logger.warning("[romantic_context] relationship_signal build failed: %s", e)
        return "(error reading relationship signals)"


def _build_food_calendar(*, now_local: datetime) -> str:
    """Already-planned meals on the Food calendar, next 14 days. Used
    to avoid double-booking date nights against family dinners."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind="food calendar")
        tool = cls()
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=14)
        tm = ToolMessage(
            tool_name="get_calendar_events",
            tool_data={
                "arguments": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "calendar_names": ["Food"],
                    "single_events": True,
                },
            },
        )
        result = tool.execute(tm)
    except Exception as e:
        logger.warning("[romantic_context] food_calendar fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="food calendar")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return "(Food calendar is empty for the next 14 days)"
    return content


def _build_wellness_calendar(*, now_local: datetime) -> str:
    """Already-scheduled wellness events to avoid clashing with."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind="wellness calendar")
        tool = cls()
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=14)
        tm = ToolMessage(
            tool_name="get_calendar_events",
            tool_data={
                "arguments": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "calendar_names": ["Wellness", "Fitness", "Workouts"],
                    "single_events": True,
                },
            },
        )
        result = tool.execute(tm)
    except Exception as e:
        logger.warning("[romantic_context] wellness_calendar fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="wellness calendar")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return "(no Wellness/Fitness calendar events in the next 14 days)"
    return content


def _build_addressable_concerns() -> str:
    """concerns_register filtered to those routed to romantic_proposer."""
    path = get_repo_root() / "resources" / "subconscious" / "resource_concerns_register.json"
    if not path.is_file():
        return "(no concerns_register yet)"
    try:
        register = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[romantic_context] concerns_register parse failed: %s", e)
        return "(error reading concerns_register)"

    matched: List[dict] = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket, []) or []:
            if "romantic_proposer" in (c.get("addressable_by") or []):
                matched.append(c)

    if not matched:
        return "(no concerns currently routed to romantic_proposer)"

    lines: List[str] = []
    for c in matched:
        # No concern_id — noticer-internal bookkeeping; proposers see prose only.
        lines.append(
            f"[{c.get('severity', '?')}/{c.get('horizon', '?')}] "
            f"{c.get('title', '(untitled)')}"
        )
        lines.append(f"  subject: {c.get('subject') or 'household'}")
        lines.append(f"  kind: {c.get('kind', '?')}")
        notes = (c.get("notes") or "").strip()
        if notes:
            lines.append(f"  notes: {notes[:400]}")
        co = [a for a in (c.get("addressable_by") or []) if a != "romantic_proposer"]
        if co:
            lines.append(f"  co-addressable: {', '.join(co)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_recent_romantic_intentions() -> str:
    """Last 30 days of intention.romantic pods."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        results = store.query(kind="intention.romantic", since="30d", limit=20)
    except Exception as e:
        logger.warning("[romantic_context] recent intentions fetch failed: %s", e)
        return "(no recent romantic intentions readable)"

    if not results:
        return "(no recent romantic intentions — propose freely if signal supports it)"

    lines: List[str] = ["Recent romantic intentions (don't repeat unless routine):"]
    for pod in results:
        meta = pod.metadata or {}
        kind = meta.get("kind", "?")
        date = meta.get("date", "?")
        summary = meta.get("summary", pod.one_liner or "")
        lines.append(f"- [{date}] {kind}: {summary}")
    return "\n".join(lines)


def _build_key_dates(*, now_local: datetime) -> str:
    """Anniversary, Katy's birthday, his birthday, kids' birthdays from
    resource_user_data. Renders relative-distance for the next 90 days."""
    try:
        user_data_path = get_repo_root() / "resources" / "user" / "resource_user_data.json"
        user_data = json.loads(user_data_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[romantic_context] user_data read failed: %s", e)
        return "(no user_data available)"

    today = now_local.date()
    horizon = today + timedelta(days=90)
    upcoming: List[str] = []

    # Jukka + Katy + kids' birthdays
    own_bd = user_data.get("birthdate")
    if own_bd:
        upcoming.extend(_next_occurrence_within(today, horizon, own_bd, f"{user_data.get('first_name', 'You')}'s birthday"))
    for person in user_data.get("important_people") or []:
        bd = person.get("birthdate")
        name = person.get("name") or "(unnamed)"
        relationship = person.get("relationship", "")
        label_extra = f" ({relationship})" if relationship else ""
        if bd:
            upcoming.extend(_next_occurrence_within(today, horizon, bd, f"{name}{label_extra} birthday"))

    # Anniversary: not in user_data today. Defer to house_rules manual entry —
    # noted in the agent's prompt that house_rules carries it. When user adds
    # it to resource_user_data, this builder reads it.
    anniversary = user_data.get("anniversary_date") or user_data.get("wedding_date")
    if anniversary:
        upcoming.extend(_next_occurrence_within(today, horizon, anniversary, "Anniversary"))

    if not upcoming:
        return "(no key dates within 90 days; house_rules may carry anniversary)"
    return "\n".join(sorted(upcoming))


def _next_occurrence_within(today, horizon, iso_date: str, label: str) -> List[str]:
    """Return one-line entries for the next yearly occurrence of `iso_date`
    that falls within [today, horizon]. Empty list if outside the window."""
    try:
        original = datetime.strptime(str(iso_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []
    candidate = original.replace(year=today.year)
    if candidate < today:
        candidate = candidate.replace(year=today.year + 1)
    if candidate > horizon:
        return []
    days = (candidate - today).days
    return [f"- {candidate.isoformat()} ({days}d away): {label}"]
