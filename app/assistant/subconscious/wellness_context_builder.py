"""Build the injected context dict for the wellness_proposer agent.

Mirrors meal_context_builder but pulls activity + sleep instead of
diet + inventory. Many helpers reused from noticer's context_builder
(calendar, family roster).

For v0, activity_log and sleep_log are stubs that try a couple of
existing data sources (KG entity descriptions, dayflow logs) and fall
back gracefully. As Emi gathers more structured fitness/sleep data
(Apple Watch ingest, sleep tracking pipeline), those slots get richer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from app.assistant.utils.identity_names import get_required_primary_user_name

from app.assistant.subconscious.context_builder import (
    _build_calendar_today_tomorrow,
    _build_family_roster,
    _resolve_household_members,
    build_weekly_schedule_block,
    _NO_DATA_FMT,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


def build_wellness_proposer_context() -> Dict[str, str]:
    """Context for wellness_proposer (daily, tactical 24-48h)."""
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "activity_log_7d": _build_activity_log(),
        "sleep_log_7d": _build_sleep_log(),
        "wellness_calendar": _build_wellness_calendar(now_local=now_local),
        "general_calendar": _build_calendar_today_tomorrow(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "addressable_concerns": _build_addressable_concerns(),
        "recent_wellness_intentions": _build_recent_wellness_intentions(),
        "weekly_schedule": build_weekly_schedule_block(),
    }


# ── section builders ─────────────────────────────────────────────────────


def _build_activity_log() -> str:
    """Recent activity/workout history. v0 stub — when Apple-Watch
    ingest or a workout log pipeline lands, this becomes structured.

    For now, scan the KG entity card description for Jukka for any
    exercise-related sentences and return them. Empty signal is fine —
    proposer adapts."""
    try:
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        from app.models.base import get_session

        session = get_session()
        try:
            primary_name = get_required_primary_user_name()
            row = (
                session.query(Node)
                .filter(Node.label == primary_name)
                .filter(Node.node_type == "Entity")
                .one_or_none()
            )
        finally:
            session.close()

        if row is None or not row.description:
            return "(no structured activity log yet — propose based on routine maintenance + concerns)"

        keywords = (
            "workout", "exercise", "bike", "cycling", "ride", "run", "running",
            "walk", "hike", "yoga", "strength", "gym", "lift", "swim", "cardio",
            "stretch", "mobility", "rest day", "active recovery",
        )
        sentences = (row.description or "").split(". ")
        relevant = [s.strip() for s in sentences if any(k in s.lower() for k in keywords)]
        if not relevant:
            return "(no exercise mentions in KG description for {0} — propose based on routine maintenance)".format(primary_name)
        return f"Activity signals for {primary_name}:\n" + "\n".join(f"- {s}." for s in relevant)
    except Exception as e:
        logger.warning("[wellness_context] activity_log build failed: %s", e)
        return "(error reading activity log)"


def _build_sleep_log() -> str:
    """Recent sleep history. v0 stub — pull from KG description as a
    fallback. When a real sleep pipeline lands (Ring snapshot ingest,
    Oura ring, etc.), this becomes structured."""
    try:
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        from app.models.base import get_session

        session = get_session()
        try:
            primary_name = get_required_primary_user_name()
            row = (
                session.query(Node)
                .filter(Node.label == primary_name)
                .filter(Node.node_type == "Entity")
                .one_or_none()
            )
        finally:
            session.close()

        if row is None or not row.description:
            return "(no structured sleep log yet — treat sleep as 'unknown', bias toward not-overloading)"

        keywords = (
            "sleep", "slept", "bedtime", "wake", "woke", "insomnia",
            "fatigue", "tired", "exhausted", "rested", "drowsy",
        )
        sentences = (row.description or "").split(". ")
        relevant = [s.strip() for s in sentences if any(k in s.lower() for k in keywords)]
        if not relevant:
            return "(no sleep mentions in KG description; treat as unknown — be conservative on intensity)"
        return f"Sleep signals for {primary_name}:\n" + "\n".join(f"- {s}." for s in relevant)
    except Exception as e:
        logger.warning("[wellness_context] sleep_log build failed: %s", e)
        return "(error reading sleep log)"


def _build_wellness_calendar(*, now_local: datetime) -> str:
    """Already-scheduled wellness events on a Fitness/Wellness calendar
    in the next 7 days. Falls back gracefully when the calendar doesn't
    exist (Jukka may not have one named Fitness yet)."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind="wellness calendar")
        tool = cls()
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        # Try common names; the tool will return what it finds. If none
        # of these calendars exist, content will be empty and we report
        # a wide-open window.
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
        logger.warning("[wellness_context] wellness_calendar fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="wellness calendar")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return "(no Wellness/Fitness calendar events in the next 7 days — wide open for proposals)"
    return content


def _build_addressable_concerns() -> str:
    """Concerns_register filtered to concerns where addressable_by
    includes wellness_proposer."""
    import json
    from app.assistant.utils.path_utils import get_repo_root

    path = get_repo_root() / "resources" / "subconscious" / "resource_concerns_register.json"
    if not path.is_file():
        return "(no concerns_register yet)"
    try:
        register = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[wellness_context] concerns_register parse failed: %s", e)
        return "(error reading concerns_register)"

    matched: List[dict] = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket, []) or []:
            if "wellness_proposer" in (c.get("addressable_by") or []):
                matched.append(c)

    if not matched:
        return "(no concerns currently routed to wellness_proposer)"

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
        co = [a for a in (c.get("addressable_by") or []) if a != "wellness_proposer"]
        if co:
            lines.append(f"  co-addressable: {', '.join(co)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_recent_wellness_intentions() -> str:
    """Read recent wellness intention pods (last 7 days) so the proposer
    doesn't repeat itself unintentionally. Includes the lane's #run_summary
    pods — a compact recap of the prior run is useful context here."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        results = store.query(kind="intention", tags=["wellness"], since="7d", limit=20)
    except Exception as e:
        logger.warning("[wellness_context] recent intentions fetch failed: %s", e)
        return "(no recent wellness intentions readable)"

    if not results:
        return "(no recent wellness intentions — propose freely)"

    lines: List[str] = ["Recent wellness intentions (don't repeat unless routine):"]
    for pod in results:
        meta = pod.metadata or {}
        kind = meta.get("kind", "?")
        date = meta.get("date", "?")
        summary = meta.get("summary", pod.one_liner or "")
        lines.append(f"- [{date}] {kind}: {summary}")
    return "\n".join(lines)
