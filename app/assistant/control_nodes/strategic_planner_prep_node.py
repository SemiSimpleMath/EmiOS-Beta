"""Pre-LLM preparation node for strategic_planner.

Loads dayflow items via ``get_dayflow_items()``, builds the context
buckets the planner needs: admitted artifacts, active items grouped
by state, plan task status, chat history, action log, and ticket state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import (
    DONE_STATES,
    get_dayflow_items,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_CHAT_HISTORY_HOURS = 12
_ACTION_LOG_HOURS = 18


def _enrich_for_prompt(item: Dict[str, Any], now_utc: datetime) -> Dict[str, Any]:
    """Wrap item with computed presentation fields, preserving metadata shape."""
    from app.assistant.dayflow_orchestrator.item_display import enrich_item_for_prompt
    return enrich_item_for_prompt(item)


def _build_plan_task_status(
    all_items: List[Dict[str, Any]],
    synopses: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build per-plan grouped view with completed/remaining tasks."""
    tasks_by_plan: Dict[str, List[Dict[str, Any]]] = {}
    for item in all_items:
        meta = get_meta(item)
        source_type = str(meta.get("source_type") or "").strip().lower()
        if source_type not in ("plan_task", "plan"):
            continue
        plan_id = str(meta.get("plan_id") or "").strip()
        if not plan_id:
            continue
        task_id = str(meta.get("short_id") or meta.get("item_id") or item.get("id") or "").strip()
        state = str(meta.get("state") or "").strip().lower()
        summary = str(meta.get("summary") or item.get("content") or "").strip()
        execution_result = str(meta.get("execution_result") or "").strip()
        if execution_result:
            lower_exec = execution_result.lower()
            if "inbound ui chat message from system" in lower_exec and "cadence tick" in lower_exec:
                execution_result = ""
        ticket_response = str(meta.get("ticket_response") or "").strip()
        dispatched_at = str(meta.get("dispatched_at_local") or meta.get("dispatched_at") or "").strip()
        task_entry: Dict[str, Any] = {
            "task_id": task_id,
            "summary": summary,
            "state": state,
        }
        if execution_result:
            task_entry["execution_result"] = execution_result
        if dispatched_at:
            task_entry["dispatched_at"] = dispatched_at
        if ticket_response:
            task_entry["ticket_response"] = ticket_response
        if state == "waiting":
            wait_reason = str(meta.get("wait_reason") or meta.get("state_reason") or "").strip()
            if wait_reason:
                task_entry["wait_reason"] = wait_reason
            reactivate = str(meta.get("reactivate_at_local") or meta.get("reactivate_at") or "").strip()
            if reactivate:
                task_entry["reactivate_at_local"] = reactivate
        tasks_by_plan.setdefault(plan_id, []).append(task_entry)

    result: List[Dict[str, Any]] = []
    for syn in synopses:
        plan_id = str(syn.get("plan_id") or "").strip()
        if not plan_id:
            continue
        all_tasks = tasks_by_plan.get(plan_id, [])
        completed = [t for t in all_tasks if t["state"] in DONE_STATES]
        remaining = [t for t in all_tasks if t["state"] not in DONE_STATES]
        plan_state = str(syn.get("state") or "active").strip().lower()
        result.append({
            "plan_id": plan_id,
            "plan_state": plan_state,
            "objective": str(syn.get("objective") or "").strip(),
            "synopsis": str(syn.get("synopsis") or "").strip(),
            "completed_tasks": completed,
            "remaining_tasks": remaining,
        })
    return result


def _load_active_tickets() -> List[Dict[str, Any]]:
    """Load active dayflow tickets from the ticket manager."""
    from app.assistant.ticket_manager import get_ticket_manager, TicketState

    _DAYFLOW_TYPES = {"dayflow_advice", "dayflow_notify", "dayflow_decision"}
    active = get_ticket_manager().get_tickets(
        states=[TicketState.PENDING, TicketState.PROPOSED, TicketState.SNOOZED],
        limit=50,
    )
    result = []
    for t in active:
        if (getattr(t, "ticket_type", "") or "").lower() not in _DAYFLOW_TYPES:
            continue
        result.append({
            "title": str(getattr(t, "title", "") or "").strip(),
            "message": str(getattr(t, "message", "") or "").strip(),
            "suggestion_type": str(getattr(t, "suggestion_type", "") or "").strip(),
            "source_task_ids": getattr(t, "source_task_ids", None) or [],
        })
    return result


def _load_responded_tickets(since_utc: datetime) -> Dict[str, List[Dict[str, Any]]]:
    """Load recent ticket responses categorized by action."""
    from app.assistant.pipelines.dayflow.utils.context_sources import (
        get_responded_tickets_categorized,
    )

    _DAYFLOW_TYPES = {"dayflow_advice", "dayflow_notify", "dayflow_decision"}
    raw = get_responded_tickets_categorized(since_utc=since_utc)
    return {
        category: [
            t for t in tickets
            if str(t.get("ticket_type") or "").lower() in _DAYFLOW_TYPES
        ]
        for category, tickets in raw.items()
    }


_WATERMARK_PATH = None  # Lazy init


def _get_watermark_path():
    global _WATERMARK_PATH
    if _WATERMARK_PATH is None:
        from app.assistant.utils.path_utils import get_repo_root
        _WATERMARK_PATH = get_repo_root() / "resources" / "status" / "planner_watermark.json"
    return _WATERMARK_PATH


def _read_watermark() -> Optional[datetime]:
    import json
    path = _get_watermark_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return parse_iso_utc(data.get("last_run_utc"))
    except Exception:
        pass
    return None


def _write_watermark(now_utc: datetime) -> None:
    import json
    path = _get_watermark_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_run_utc": now_utc.isoformat()}), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write planner watermark: %s", e)


def _build_recent_changes(
    all_items: List[Dict[str, Any]],
    since_utc: Optional[datetime],
    now_utc: datetime,
) -> List[str]:
    """Build a list of material changes since the last planner run."""
    if since_utc is None:
        return []

    local_tz = get_local_timezone()
    changes: List[str] = []

    for item in all_items:
        meta = get_meta(item)
        source_type = str(meta.get("source_type") or "").strip().lower()
        if source_type in ("action_dispatch", "action_result"):
            continue

        # Check if this item was created or changed since the watermark.
        last_reviewed = parse_iso_utc(str(meta.get("last_reviewed_at") or ""))
        created = parse_iso_utc(str(meta.get("created_at") or ""))

        is_recent = False
        if last_reviewed and last_reviewed >= since_utc:
            is_recent = True
        elif created and created >= since_utc:
            is_recent = True

        if not is_recent:
            continue

        state = str(meta.get("state") or "").strip().lower()
        if state == "suppressed":
            continue

        sid = str(meta.get("short_id") or "").strip()
        summary = str(meta.get("summary") or "").strip()
        plan_id = str(meta.get("plan_id") or "").strip()
        ts = last_reviewed or created
        time_local = ts.astimezone(local_tz).strftime("%I:%M %p") if ts else ""

        # Determine what kind of change this was.
        state_reason = str(meta.get("state_reason") or "").strip()

        if source_type == "chat":
            changes.append(f"[{time_local}] User said: {summary}")
        elif source_type == "plan_synopsis":
            plan_state = str(meta.get("plan_status") or state).strip()
            changes.append(f"[{time_local}] Plan [{plan_id}] {plan_state}: {summary}")
        elif source_type == "action_log":
            changes.append(f"[{time_local}] {summary}")
        else:
            label = f"task {sid}" if sid else source_type
            plan_tag = f" | plan: {plan_id}" if plan_id else ""
            changes.append(f"[{time_local}] {label} → {state}{plan_tag}: {summary}")

    changes.sort()
    return changes[:25]


class StrategicPlannerPrepNode(ControlNode):
    """Pre-LLM node that prepares context for strategic_planner.

    Builds all context the planner needs from get_dayflow_items() plus
    ticket manager lookups. Resource-based context (calendar, weather,
    health, AFK, etc.) is resolved by the context injector from the
    agent's config.yaml — not prepared here.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)
        watermark_utc = _read_watermark()

        all_items = get_dayflow_items()

        # Split items into buckets.
        active_items: List[Dict[str, Any]] = []
        plan_synopses: List[Dict[str, Any]] = []
        chat_history: List[Dict[str, Any]] = []
        action_log: List[Dict[str, Any]] = []

        chat_cutoff = now_utc - timedelta(hours=_CHAT_HISTORY_HOURS)
        action_cutoff = now_utc - timedelta(hours=_ACTION_LOG_HOURS)
        local_tz = get_local_timezone()

        for item in all_items:
            meta = get_meta(item)
            source_type = str(meta.get("source_type") or "").strip().lower()

            if source_type == "plan_synopsis":
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

            # Dispatch/result log entries are already represented in the action log.
            if source_type in ("action_dispatch", "action_result"):
                continue

            # Everything else is active context.
            active_items.append(_enrich_for_prompt(item, now_utc))

        # Admitted artifacts from triage (written by triage_persist_node).
        admitted = self.blackboard.get_state_value("admitted_artifacts", []) or []

        # Plan task status.
        plan_task_status = _build_plan_task_status(all_items, plan_synopses)

        # Tickets from ticket manager (not dayflow items).
        active_tickets = _load_active_tickets()
        responded_tickets = _load_responded_tickets(
            since_utc=now_utc - timedelta(hours=2),
        )

        # Recent changes since last planner run.
        recent_changes = _build_recent_changes(all_items, watermark_utc, now_utc)

        # Write to blackboard.
        self.blackboard.update_state_value("admitted_artifacts", admitted)
        self.blackboard.update_state_value("active_dayflow_items", active_items)
        self.blackboard.update_state_value("active_plan_synopses", plan_synopses)
        self.blackboard.update_state_value("plan_task_status", plan_task_status)
        self.blackboard.update_state_value("recent_dayflow_chat_history", chat_history)
        self.blackboard.update_state_value("recent_action_selector_actions", action_log)
        self.blackboard.update_state_value("active_tickets", active_tickets)
        self.blackboard.update_state_value("recent_responded_tickets", responded_tickets)
        self.blackboard.update_state_value("recent_changes", recent_changes)

        # Watermark is written by PlannerPersistNode after planner output
        # is saved, so the planner's own changes don't appear as "recent".

        logger.info(
            "[%s] prepared: active=%d synopses=%d admitted=%d chat=%d actions=%d tickets=%d changes=%d",
            self.name,
            len(active_items),
            len(plan_synopses),
            len(admitted),
            len(chat_history),
            len(action_log),
            len(active_tickets),
            len(recent_changes),
        )
        self.blackboard.update_state_value("last_agent", self.name)
