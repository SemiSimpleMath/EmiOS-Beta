"""Build the injected context dict for the scheduler_arbiter agent.

Aggregates EVERY fresh candidate intention.* pod across all proposers
(meal, wellness, romantic) for the next 14 days, plus the household
calendar (hard constraints), key dates, family roster, and the previous
arbiter run's plan.weekly_schedule (if any).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.subconscious.context_builder import (
    _build_calendar_today_tomorrow,
    _build_family_roster,
    _resolve_household_members,
    _NO_DATA_FMT,
)
from app.assistant.subconscious.romantic_context_builder import _build_key_dates
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


# Pod kinds the arbiter considers candidates. Adding a future domain
# (family_proposer, finance_proposer) just means appending here.
_INTENTION_KINDS = [
    "intention.meal",
    "intention.wellness",
    "intention.romantic",
]


def build_scheduler_arbiter_context() -> Dict[str, str]:
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "week_start_date": _compute_week_start_date(now_local),
        "candidate_intentions": _build_candidate_intentions(now_local=now_local),
        "existing_schedule": _build_existing_schedule(),
        "key_dates": _build_key_dates(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "household_calendar": _build_household_calendar(now_local=now_local),
    }


def _compute_week_start_date(now_local: datetime) -> str:
    days_since_monday = now_local.weekday()
    return (now_local - timedelta(days=days_since_monday)).date().isoformat()


def _build_candidate_intentions(*, now_local: datetime) -> str:
    """Pull every intention.* pod whose `metadata.date` is in
    [today, today+14d]. Pods without a date metadata field are still
    included but flagged."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        horizon = (now_local + timedelta(days=14)).date()
        today = now_local.date()
        pods: List[Any] = []
        for kind in _INTENTION_KINDS:
            pods.extend(store.query(kind=kind, since="14d", limit=100))
    except Exception as e:
        logger.warning("[arbiter_context] intention pod fetch failed: %s", e)
        return "(no intention pods readable)"

    if not pods:
        return "(no candidate intentions for the next 14 days)"

    in_window: List[Dict[str, Any]] = []
    out_of_window: int = 0
    for pod in pods:
        meta = pod.metadata or {}
        d_raw = (meta.get("date") or "").strip()
        try:
            d = datetime.strptime(d_raw, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            d = None
        if d is None:
            continue
        if d < today or d > horizon:
            out_of_window += 1
            continue
        in_window.append({"pod": pod, "date": d, "meta": meta})

    if not in_window:
        return f"(no candidate intentions in the next 14 days; {out_of_window} pods outside window)"

    in_window.sort(key=lambda x: (x["date"].isoformat(), x["meta"].get("kind") or "", x["pod"].pod_id))

    lines: List[str] = [f"# {len(in_window)} candidate intentions (next 14 days)", ""]
    for it in in_window:
        pod = it["pod"]
        meta = it["meta"]
        domain = pod.kind.split(".")[-1] if "." in pod.kind else pod.kind
        kind = meta.get("kind") or domain
        actors = meta.get("actors") or []
        date = meta.get("date") or "?"
        start_local = meta.get("proposed_start_local")
        duration = meta.get("duration_minutes")
        summary = meta.get("summary") or pod.one_liner or "(no summary)"

        head = f"- [{date}] {domain}.{kind}"
        if start_local:
            head += f" @ {start_local}"
        if duration:
            head += f" ({duration} min)"
        if actors:
            head += f" — actors: {', '.join(actors)}"
        lines.append(head)
        lines.append(f"  pod: {pod.pod_id}")
        lines.append(f"  summary: {summary}")
        if meta.get("confidence"):
            lines.append(f"  confidence: {meta.get('confidence')}  • novelty: {meta.get('novelty', '?')}")
        if meta.get("advance_required_days") is not None:
            lines.append(f"  advance_required_days: {meta.get('advance_required_days')}")
        if meta.get("requires_babysitter"):
            lines.append(f"  requires_babysitter: yes")
        if meta.get("cost_estimate_usd") is not None:
            lines.append(f"  est_cost_usd: {meta.get('cost_estimate_usd')}")
        if meta.get("intensity"):
            lines.append(f"  intensity: {meta.get('intensity')}")
        if meta.get("addresses_concern_ids"):
            lines.append(f"  addresses: {', '.join(meta.get('addresses_concern_ids') or [])}")
        lines.append("")
    if out_of_window:
        lines.append(f"({out_of_window} more intentions outside the 14-day window — not considered)")
    return "\n".join(lines).rstrip()


def _build_existing_schedule() -> str:
    """The most recent plan.weekly_schedule pod, if any. Reference for
    what the arbiter committed to last run."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        results = store.query(kind="plan.weekly_schedule", since="10d", limit=1)
    except Exception as e:
        logger.warning("[arbiter_context] existing schedule fetch failed: %s", e)
        return "(no existing schedule readable)"

    if not results:
        return "(no prior weekly schedule — this is the first arbiter run)"
    latest = results[0]
    return f"{latest.one_liner}\n\n{latest.body}"


def _build_household_calendar(*, now_local: datetime) -> str:
    """All Google Calendar events for the next 14 days. These are HARD
    constraints — events the user already put on the calendar."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind="household calendar")
        tool = cls()
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=14)
        tm = ToolMessage(
            tool_name="get_calendar_events",
            tool_data={
                "arguments": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "single_events": True,
                },
            },
        )
        result = tool.execute(tm)
    except Exception as e:
        logger.warning("[arbiter_context] household_calendar fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="household calendar")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return "(no calendar events in the next 14 days)"
    return content
