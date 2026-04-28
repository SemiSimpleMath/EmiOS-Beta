"""Pre-LLM preparation node for relevance_cleaner.

Loads dayflow items via ``get_dayflow_items()`` and builds the context
the cleaner needs: items with timestamps showing when they were closed,
plan status (active/closed), and today's action log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import (
    DONE_STATES,
    get_dayflow_items,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_ACTION_LOG_HOURS = 18


def _compute_closed_ago(meta: Dict[str, Any], now_utc: datetime) -> str:
    """Compute how long ago an item was closed/acted_on."""
    raw = str(meta.get("last_reviewed_at") or "").strip()
    if not raw:
        return ""
    parsed = parse_iso_utc(raw)
    if parsed is None:
        return ""
    delta = int((now_utc - parsed).total_seconds())
    if delta < 0:
        return ""
    h, m = divmod(delta // 60, 60)
    if h > 0:
        return f"{h}h {m}m ago"
    return f"{m}m ago"


class RelevanceCleanerPrepNode(ControlNode):
    """Pre-LLM node that prepares context for relevance_cleaner.

    Builds:
    - active_dayflow_items with closed_ago timestamps so the cleaner
      knows how old closed items are
    - plan_task_status with plan state (active/closed) so the cleaner
      can confidently suppress closed plans
    - recent action log
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)
        local_tz = get_local_timezone()

        all_items = get_dayflow_items()

        active_items: List[Dict[str, Any]] = []
        plan_synopses: List[Dict[str, Any]] = []
        action_log: List[Dict[str, Any]] = []

        action_cutoff = now_utc - timedelta(hours=_ACTION_LOG_HOURS)

        for item in all_items:
            meta = get_meta(item)
            source_type = str(meta.get("source_type") or "").strip().lower()
            state = str(meta.get("state") or "").strip().lower()

            if source_type == "plan_synopsis":
                plan_synopses.append(meta)
                continue

            if source_type == "chat":
                continue

            if source_type in ("action_log", "action_dispatch", "action_result"):
                if source_type == "action_log":
                    created = parse_iso_utc(str(meta.get("created_at") or ""))
                    if created is not None and created >= action_cutoff:
                        action_log.append({
                            "time_local": str(meta.get("time_local") or "").strip(),
                            "summary": str(meta.get("summary") or "").strip(),
                            "plan_id": str(meta.get("plan_id") or "").strip(),
                            "task_id": str(meta.get("task_id") or "").strip(),
                        })
                continue

            # Enrich with closed_ago so the cleaner knows how stale items are.
            enriched_meta = dict(meta)
            if state in ("closed", "acted_on"):
                enriched_meta["closed_ago"] = _compute_closed_ago(meta, now_utc)
                # Add local time of closure.
                raw_reviewed = str(meta.get("last_reviewed_at") or "").strip()
                parsed_reviewed = parse_iso_utc(raw_reviewed)
                if parsed_reviewed is not None:
                    enriched_meta["closed_at_local"] = parsed_reviewed.astimezone(local_tz).strftime(
                        "%I:%M %p"
                    )

            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            active_items.append({"id": item.get("id", item_id), "metadata": enriched_meta})

        # Plan task status with plan state.
        tasks_by_plan: Dict[str, List[Dict[str, Any]]] = {}
        for item in all_items:
            meta = get_meta(item)
            st = str(meta.get("source_type") or "").strip().lower()
            if st not in ("plan_task", "plan"):
                continue
            plan_id = str(meta.get("plan_id") or "").strip()
            if not plan_id:
                continue
            state = str(meta.get("state") or "").strip().lower()
            tasks_by_plan.setdefault(plan_id, []).append({
                "task_id": str(meta.get("short_id") or meta.get("item_id") or "").strip(),
                "summary": str(meta.get("summary") or "").strip(),
                "state": state,
            })

        plan_task_status: List[Dict[str, Any]] = []
        for syn in plan_synopses:
            plan_id = str(syn.get("plan_id") or "").strip()
            if not plan_id:
                continue
            plan_state = str(syn.get("state") or "").strip().lower()
            all_tasks = tasks_by_plan.get(plan_id, [])
            completed = [t for t in all_tasks if t["state"] in DONE_STATES]
            remaining = [t for t in all_tasks if t["state"] not in DONE_STATES]

            entry: Dict[str, Any] = {
                "plan_id": plan_id,
                "plan_state": plan_state,
                "objective": str(syn.get("objective") or "").strip(),
                "completed_count": len(completed),
                "remaining_count": len(remaining),
            }
            # Add closed_ago for closed plans.
            if plan_state == "closed":
                entry["closed_ago"] = _compute_closed_ago(syn, now_utc)
            plan_task_status.append(entry)

        self.blackboard.update_state_value("active_dayflow_items", active_items)
        self.blackboard.update_state_value("plan_task_status", plan_task_status)
        self.blackboard.update_state_value("recent_action_selector_actions", action_log)

        logger.info(
            "[%s] prepared: active=%d plans=%d actions=%d",
            self.name,
            len(active_items),
            len(plan_task_status),
            len(action_log),
        )
        self.blackboard.update_state_value("last_agent", self.name)
