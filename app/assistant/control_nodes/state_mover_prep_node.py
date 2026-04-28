"""Pre-LLM preparation node for state_mover.

Refreshes the active items view from ``get_dayflow_items()`` after
planner persistence, and prepares chat history and ticket responses
for state transition decisions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_CHAT_HISTORY_HOURS = 6


def _enrich_for_prompt(item: Dict[str, Any], now_utc: datetime) -> Dict[str, Any]:
    """Wrap item with computed presentation fields, preserving metadata shape."""
    from app.assistant.dayflow_orchestrator.item_display import enrich_item_for_prompt
    return enrich_item_for_prompt(item)


class StateMoverPrepNode(ControlNode):
    """Pre-LLM node that prepares context for state_mover.

    Refreshes the items view after planner persistence so state_mover
    sees newly created plan tasks. Also loads chat and ticket responses
    for context.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)
        local_tz = get_local_timezone()

        all_items = get_dayflow_items()

        active_items: List[Dict[str, Any]] = []
        plan_synopses: List[Dict[str, Any]] = []
        chat_history: List[Dict[str, Any]] = []

        chat_cutoff = now_utc - timedelta(hours=_CHAT_HISTORY_HOURS)

        for item in all_items:
            meta = get_meta(item)
            source_type = str(meta.get("source_type") or "").strip().lower()

            if source_type == "plan_synopsis":
                # Only include active plans — closed plans were completed by the planner.
                state = str(meta.get("state") or "").strip().lower()
                if state not in ("closed", "suppressed"):
                    plan_synopses.append(meta)
                continue

            if source_type == "chat":
                created = parse_iso_utc(str(meta.get("created_at") or ""))
                if created is not None and created >= chat_cutoff:
                    time_local = created.astimezone(local_tz).strftime("%I:%M %p")
                    chat_history.append({
                        "time_local": time_local,
                        "summary": str(meta.get("summary") or "").strip(),
                    })
                continue

            if source_type in ("action_log", "action_dispatch", "action_result"):
                continue

            active_items.append(_enrich_for_prompt(item, now_utc))

        # Convert reactivate_at to local time in planned_tasks so the
        # state_mover and its prompts see local times, not UTC.
        planned_tasks = self.blackboard.get_state_value("planned_tasks", []) or []
        if isinstance(planned_tasks, list):
            local_tz = get_local_timezone()
            for task in planned_tasks:
                if not isinstance(task, dict):
                    continue
                raw = task.get("reactivate_at")
                if raw and not task.get("reactivate_at_local"):
                    parsed = parse_iso_utc(str(raw))
                    if parsed is not None:
                        task["reactivate_at_local"] = parsed.astimezone(local_tz).strftime("%Y-%m-%d %I:%M %p")
            self.blackboard.update_state_value("planned_tasks", planned_tasks)

        # Ticket responses for context.
        from app.assistant.pipelines.dayflow.utils.context_sources import (
            get_responded_tickets_categorized,
        )
        _DAYFLOW_TYPES = {"dayflow_advice", "dayflow_notify", "dayflow_decision"}
        raw_responded = get_responded_tickets_categorized(
            since_utc=now_utc - timedelta(hours=2),
        )
        responded_tickets = {
            category: [
                t for t in tickets
                if str(t.get("ticket_type") or "").lower() in _DAYFLOW_TYPES
            ]
            for category, tickets in raw_responded.items()
        }

        self.blackboard.update_state_value("active_dayflow_items", active_items)
        self.blackboard.update_state_value("active_plan_synopses", plan_synopses)
        self.blackboard.update_state_value("recent_dayflow_chat_history", chat_history)
        self.blackboard.update_state_value("recent_responded_tickets", responded_tickets)

        logger.info(
            "[%s] prepared: active=%d synopses=%d chat=%d",
            self.name,
            len(active_items),
            len(plan_synopses),
            len(chat_history),
        )
        self.blackboard.update_state_value("last_agent", self.name)
