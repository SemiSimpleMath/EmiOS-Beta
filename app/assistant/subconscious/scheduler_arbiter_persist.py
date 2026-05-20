"""Persist scheduler_arbiter output.

Single output: ONE plan.weekly_schedule pod per run. This pod is the
authoritative weekly source of truth that proposers honor on their
next run.

For conflicts_for_user, surface a dayflow ticket (one per conflict).
The user answers via the ticket; the next arbiter run will read the
answered concern and re-resolve.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_scheduler_arbiter_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Mint a plan.weekly_schedule pod + surface user-tickets."""
    store = PodStore()
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    week_start_date = output.get("week_start_date") or "?"
    schedule = output.get("weekly_schedule") or []
    resolved = output.get("conflicts_resolved") or []
    user_conflicts = output.get("conflicts_for_user") or []
    free_form_thinking = (output.get("free_form_thinking") or "").strip()

    schedule_pod_id = _mint_weekly_schedule_pod(
        store=store,
        week_start_date=week_start_date,
        schedule=schedule,
        resolved=resolved,
        user_conflicts=user_conflicts,
        free_form_thinking=free_form_thinking,
        now_utc_iso=now_utc_iso,
    )

    tickets_created: List[str] = []
    for conflict in user_conflicts:
        ticket_id = _surface_user_conflict_ticket(conflict, schedule_pod_id=schedule_pod_id)
        if ticket_id:
            tickets_created.append(ticket_id)

    return {
        "schedule_pod_id": schedule_pod_id,
        "schedule_items": len(schedule),
        "anchored_items": sum(1 for s in schedule if s.get("is_anchor")),
        "conflicts_resolved": len(resolved),
        "conflicts_for_user": len(user_conflicts),
        "tickets_created": tickets_created,
    }


def _mint_weekly_schedule_pod(
    *,
    store: PodStore,
    week_start_date: str,
    schedule: List[Dict[str, Any]],
    resolved: List[Dict[str, Any]],
    user_conflicts: List[Dict[str, Any]],
    free_form_thinking: str,
    now_utc_iso: str,
) -> Optional[str]:
    try:
        pod_id = f"datapod:plan.weekly_schedule:{uuid.uuid4().hex[:24]}"

        anchor_count = sum(1 for s in schedule if s.get("is_anchor"))
        one_liner = (
            f"Week of {week_start_date} — {len(schedule)} items "
            f"({anchor_count} anchored, "
            f"{len(resolved)} conflicts resolved, "
            f"{len(user_conflicts)} for user)"
        )

        # Body: human-readable schedule grouped by date, plus resolution + punt sections
        body_parts = [
            f"# Weekly schedule — week of {week_start_date}",
            "",
            f"**Generated:** {now_utc_iso}",
            "",
        ]

        # Schedule by day
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for item in schedule:
            d = item.get("date") or "?"
            by_date.setdefault(d, []).append(item)

        if by_date:
            body_parts.append("## Schedule")
            for date in sorted(by_date.keys()):
                body_parts.append(f"### {date}")
                for item in by_date[date]:
                    anchor_marker = " 🔒" if item.get("is_anchor") else ""
                    summary = item.get("summary") or "?"
                    domain = item.get("domain") or "?"
                    actors = item.get("actors") or []
                    start = item.get("proposed_start_local")
                    duration = item.get("duration_minutes")
                    head = f"- **[{domain}]**{anchor_marker} {summary}"
                    if start:
                        head += f" @ {start}"
                    if duration:
                        head += f" ({duration}min)"
                    if actors:
                        head += f" — {', '.join(actors)}"
                    body_parts.append(head)
                    if item.get("source_intention_pod_id"):
                        body_parts.append(f"  source: {item['source_intention_pod_id']}")
                body_parts.append("")

        if resolved:
            body_parts.append("## Conflicts resolved")
            for c in resolved:
                body_parts.append(f"- **{c.get('conflict_summary')}**")
                body_parts.append(f"  chose: {c.get('chosen_pod_id')}")
                if c.get("displaced_pod_ids"):
                    body_parts.append(f"  displaced: {', '.join(c['displaced_pod_ids'])}")
                if c.get("priority_rule_applied"):
                    body_parts.append(f"  rule: {c['priority_rule_applied']}")
                if c.get("reasoning"):
                    body_parts.append(f"  reasoning: {c['reasoning']}")
                body_parts.append("")

        if user_conflicts:
            body_parts.append("## Conflicts punted to user")
            for c in user_conflicts:
                body_parts.append(f"- **{c.get('conflict_summary')}**")
                for opt in c.get("options") or []:
                    body_parts.append(f"    • {opt}")
                if c.get("reasoning_for_punt"):
                    body_parts.append(f"  reason for punt: {c['reasoning_for_punt']}")
                body_parts.append("")

        if free_form_thinking:
            body_parts.append("## Arbiter thinking")
            body_parts.append(free_form_thinking)

        pod = Pod(
            pod_id=pod_id,
            kind="plan.weekly_schedule",
            tags=["plan", "weekly_schedule"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[],
            for_agents=[
                "meal_proposer", "daily_meal_proposer", "weekly_meal_planner",
                "wellness_proposer", "romantic_proposer",
            ],
            scope_id=None,
            created_by="scheduler_arbiter",
            metadata={
                "produced_at_utc": now_utc_iso,
                "week_start_date": week_start_date,
                "schedule": schedule,
                "conflicts_resolved": resolved,
                "conflicts_for_user": user_conflicts,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[arbiter_persist] mint plan.weekly_schedule failed: %s", e)
        return None


def _surface_user_conflict_ticket(
    conflict: Dict[str, Any],
    *,
    schedule_pod_id: Optional[str],
) -> Optional[str]:
    """One dayflow ticket per unresolved conflict. The arbiter punted —
    ask the user."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage

        cls = DI.tool_registry.get_tool_class("create_dayflow_ticket")
        if cls is None:
            logger.warning("[arbiter_persist] create_dayflow_ticket unavailable; skipping ticket")
            return None
        tool = cls()

        summary = conflict.get("conflict_summary") or "Scheduling conflict"
        options = conflict.get("options") or []
        reasoning = conflict.get("reasoning_for_punt") or ""
        candidate_ids = conflict.get("candidate_pod_ids") or []

        body_lines = ["Conflict needing your call:", "", summary, ""]
        if options:
            body_lines.append("Options:")
            for opt in options:
                body_lines.append(f"- {opt}")
            body_lines.append("")
        if reasoning:
            body_lines.append(f"Why I'm asking: {reasoning}")
        if candidate_ids:
            body_lines.append("")
            body_lines.append(f"Candidate intentions: {', '.join(candidate_ids)}")
        if schedule_pod_id:
            body_lines.append(f"Schedule pod: {schedule_pod_id}")

        title = f"Schedule conflict: {summary}"[:120]
        tm = ToolMessage(
            tool_name="create_dayflow_ticket",
            tool_data={
                "arguments": {
                    "ticket_kind": "info",
                    "suggestion_type": "scheduler_conflict",
                    "title": title,
                    "message": "\n".join(body_lines)[:1200],
                    "trigger_context": {
                        "schedule_pod_id": schedule_pod_id,
                        "candidate_pod_ids": candidate_ids,
                    },
                    "trigger_reason": "scheduler_arbiter_user_conflict",
                    "valid_hours": 48,
                },
            },
        )
        result = tool.execute(tm)
        data = getattr(result, "data", None) or {}
        return str(data.get("ticket_id") or "") or None
    except Exception as e:
        logger.warning("[arbiter_persist] surface user conflict ticket failed: %s", e)
        return None
