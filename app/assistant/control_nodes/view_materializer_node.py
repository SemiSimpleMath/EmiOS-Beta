from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_IMPORTANCE_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_RECENTLY_ACTED_ON_HOURS = 2
_CONTEXT_RECENCY_HOURS = 8
_STALENESS_HOURS = 12

_STATE_TO_BUCKET = {
    "actionable": "actionable_items",
    "dispatched": "recently_acted_on",
    "important_open": "watching_upcoming",
    "needs_planning": "watching_upcoming",
    "artifact": "artifacts_context",
    "new": "artifacts_context",
    "watching": "watching_upcoming",
    "waiting": "watching_upcoming",
    "acted_on": "recently_acted_on",
    "suppressed": "useful_recent_context",
}


def _utc_to_local_str(utc_str: str, now_utc: datetime) -> Tuple[str, str]:
    """Convert a UTC ISO string to (local_time_str, relative_str).

    Returns ("", "") if the input is empty or unparseable.
    """
    parsed = parse_iso_utc(utc_str)
    if parsed is None:
        return ("", "")
    local_tz = get_local_timezone()
    local_dt = parsed.astimezone(local_tz)
    local_str = local_dt.strftime("%Y-%m-%d %H:%M %Z")
    delta = parsed - now_utc
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        abs_secs = abs(total_seconds)
        h, m = divmod(abs_secs // 60, 60)
        relative = f"{h}h {m}m ago" if h else f"{m}m ago"
    else:
        h, m = divmod(total_seconds // 60, 60)
        relative = f"in {h}h {m}m" if h else f"in {m}m"
    return (local_str, relative)


def _importance_sort_key(item: Dict[str, Any]) -> Tuple[int, str]:
    imp = str(item.get("importance") or "medium").strip().lower()
    return (_IMPORTANCE_RANK.get(imp, 2), item.get("item_id", ""))


def _start_utc_sort_key(item: Dict[str, Any]) -> Tuple[str, str]:
    return (item.get("scheduled_start_local") or "9999", item.get("item_id", ""))


def _reviewed_sort_key(item: Dict[str, Any]) -> str:
    return item.get("_last_reviewed_at_raw") or "0000"


def _is_fresh(meta: Dict[str, Any], cutoff: datetime) -> bool:
    """Return True if the item's most recent timestamp is newer than *cutoff*.

    Checks ``last_reviewed_at`` and ``created_at``.
    Items with no parseable timestamp are kept (fail-open).
    """
    best: datetime | None = None
    for key in ("last_reviewed_at", "created_at"):
        raw = meta.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        parsed = parse_iso_utc(raw)
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    if best is None:
        return True
    return best >= cutoff


def _build_view_item(item_id: str, meta: Dict[str, Any], state: str,
                     now_utc: datetime) -> Dict[str, Any]:
    start_utc_raw = str(meta.get("scheduled_start_utc") or "").strip()
    end_utc_raw = str(meta.get("scheduled_end_utc") or "").strip()
    reviewed_utc_raw = str(meta.get("last_reviewed_at") or "").strip()
    start_local, start_relative = _utc_to_local_str(start_utc_raw, now_utc)
    end_local, end_relative = _utc_to_local_str(end_utc_raw, now_utc)
    reviewed_local, reviewed_relative = _utc_to_local_str(reviewed_utc_raw, now_utc)

    view = {
        "item_id": item_id,
        "short_id": str(meta.get("short_id") or item_id),
        "summary": str(meta.get("summary") or "").strip(),
        "source_type": str(meta.get("source_type") or "").strip(),
        "event_type": str(meta.get("event_type") or "").strip(),
        "state": state,
        "importance": str(meta.get("importance") or "medium").strip(),
        "actionability": str(meta.get("actionability") or "").strip(),
        "cooldown_until": str(meta.get("cooldown_until") or "").strip(),
        "scheduled_start_local": start_local,
        "starts_in": start_relative,
        "scheduled_end_local": end_local,
        "ends_in": end_relative,
        "last_reviewed_local": reviewed_local,
        "last_reviewed_relative": reviewed_relative,
        "wait_reason": str(meta.get("wait_reason") or "").strip(),
        "reactivate_at_utc": str(meta.get("reactivate_at_utc") or "").strip(),
        "wake_signals": meta.get("wake_signals") if isinstance(meta.get("wake_signals"), list) else [],
        "blocked_by": meta.get("blocked_by") if isinstance(meta.get("blocked_by"), list) else [],
        "priority_on_wake": str(meta.get("priority_on_wake") or "").strip(),
        "_last_reviewed_at_raw": str(meta.get("last_reviewed_at") or "").strip(),
    }
    if str(meta.get("source_type") or "").strip() == "email":
        email_sender = str(meta.get("email_sender") or "Unknown").strip() or "Unknown"
        email_subject = str(meta.get("email_subject") or view["summary"]).strip() or view["summary"]
        email_summary = str(meta.get("email_summary") or "").strip()
        created_local = str(meta.get("created_at_local") or "").strip()
        if not created_local:
            created_utc = parse_iso_utc(meta.get("created_at"))
            if created_utc is not None:
                created_local = created_utc.astimezone(get_local_timezone()).strftime("%I:%M %p")
        view["email_sender"] = email_sender
        view["email_subject"] = email_subject
        view["email_summary"] = email_summary
        view["email_created_local"] = created_local
        rich_summary = f'Email received{(" " + created_local) if created_local else ""} from {email_sender}: "{email_subject}"'
        if email_summary:
            rich_summary = f"{rich_summary} — {email_summary}"
        view["summary"] = rich_summary
    plan_id = str(meta.get("plan_id") or "").strip()
    if plan_id:
        view["plan_id"] = plan_id
    task_id = str(meta.get("task_id") or "").strip()
    if task_id:
        view["task_id"] = task_id
    action_desc = str(meta.get("action_description") or "").strip()
    if action_desc:
        view["action_description"] = action_desc
    condition = str(meta.get("condition") or "").strip()
    if condition:
        view["condition"] = condition
    return view


class ViewMaterializerNode(ControlNode):
    """
    Deterministic view builder for the action_selector.

    Reads existing DB items, applies state_mover mutations virtually, adds
    spawned items, then sorts everything into view buckets by post-mutation
    state. No LLM involved.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)
        recency_cutoff = now_utc - timedelta(hours=_RECENTLY_ACTED_ON_HOURS)
        staleness_cutoff = now_utc - timedelta(hours=_STALENESS_HOURS)

        from app.assistant.dayflow_orchestrator.contracts import (
            validate_dayflow_items,
            validate_mutations,
            validate_planned_tasks,
        )

        existing = get_dayflow_items()
        validate_dayflow_items(existing, f"{self.name}::existing_dayflow_items")

        mutations = self.blackboard.get_state_value("state_mutations", []) or []
        if not isinstance(mutations, list):
            raise ValueError(f"[{self.name}] state_mutations must be a list.")
        validate_mutations(mutations, f"{self.name}::state_mutations")

        planned_tasks_raw = self.blackboard.get_state_value("planned_tasks", []) or []
        validate_planned_tasks(planned_tasks_raw, f"{self.name}::planned_tasks")

        mutation_map: Dict[str, Dict[str, Any]] = {}
        for mut in mutations:
            if not isinstance(mut, dict):
                continue
            mid = str(mut.get("item_id") or "").strip()
            to_state = str(mut.get("to_state") or "").strip()
            if mid and to_state:
                mutation_map[mid] = mut

        all_spawns: List[Dict[str, Any]] = []
        if isinstance(planned_tasks_raw, list):
            for task in planned_tasks_raw:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("task_id") or "").strip()
                if not task_id:
                    continue
                mapped_state = "important_open"
                all_spawns.append({
                    "item_id": task_id,
                    "summary": str(task.get("task") or "").strip(),
                    "source_type": "plan_task",
                    "event_type": "planned_task",
                    "importance": "medium",
                    "actionability": "actionable",
                    "state": mapped_state,
                    "plan_id": str(task.get("plan_id") or "").strip(),
                    "blocked_by": task.get("dependencies") if isinstance(task.get("dependencies"), list) else [],
                    "wake_signals": task.get("wake_signals") if isinstance(task.get("wake_signals"), list) else [],
                    "reactivate_at_utc": str(task.get("reactivate_at_utc") or task.get("reactivate_at") or "").strip(),
                    "priority_on_wake": str(task.get("priority_on_wake") or "").strip(),
                    "wait_reason": str(task.get("wait_reason") or "").strip(),
                })

        items_by_id: Dict[str, Tuple[Dict[str, Any], str]] = {}
        stale_dropped = 0

        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                continue
            if str(meta.get("source_type") or "").strip() == "chat":
                continue
            if not _is_fresh(meta, staleness_cutoff):
                stale_dropped += 1
                continue

            mut = mutation_map.get(item_id)
            if mut:
                post_state = str(mut.get("to_state") or meta.get("state") or "").strip().lower()
                merged_meta = dict(meta)
                for ext_key in ("wait_reason", "reactivate_at_utc", "reactivate_at", "wake_signals", "blocked_by", "priority_on_wake"):
                    val = mut.get(ext_key)
                    if val is not None and val != "" and val != []:
                        merged_meta[ext_key] = val
                items_by_id[item_id] = (merged_meta, post_state)
            else:
                state = str(meta.get("state") or "").strip().lower()
                items_by_id[item_id] = (meta, state)

        spawn_seen: set = set()
        for raw in all_spawns:
            if not isinstance(raw, dict):
                continue
            sid = str(raw.get("item_id") or "").strip()
            if not sid or sid in items_by_id or sid in spawn_seen:
                continue
            spawn_seen.add(sid)
            state = str(raw.get("state") or "new").strip().lower()
            items_by_id[sid] = (raw, state)

        actionable_items: List[Dict[str, Any]] = []
        artifacts_context: List[Dict[str, Any]] = []
        watching_upcoming: List[Dict[str, Any]] = []
        recently_acted_on: List[Dict[str, Any]] = []
        context_candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

        context_recency_cutoff = now_utc - timedelta(hours=_CONTEXT_RECENCY_HOURS)

        for item_id, (meta, state) in items_by_id.items():
            bucket = _STATE_TO_BUCKET.get(state)
            if bucket is None:
                continue

            view_item = _build_view_item(item_id, meta, state, now_utc)

            if bucket == "actionable_items":
                actionable_items.append(view_item)
            elif bucket == "artifacts_context":
                artifacts_context.append(view_item)
            elif bucket == "watching_upcoming":
                watching_upcoming.append(view_item)
            elif bucket == "recently_acted_on":
                last_reviewed = parse_iso_utc(meta.get("last_reviewed_at"))
                if last_reviewed and last_reviewed >= recency_cutoff:
                    recently_acted_on.append(view_item)
            elif bucket == "useful_recent_context":
                context_candidates.append((view_item, meta))

        actionable_items.sort(key=_importance_sort_key)
        artifacts_context.sort(key=_start_utc_sort_key)
        watching_upcoming.sort(key=_start_utc_sort_key)
        recently_acted_on.sort(key=_reviewed_sort_key, reverse=True)

        seen_summaries: set = set()
        for vi in actionable_items + artifacts_context + watching_upcoming + recently_acted_on:
            s = vi.get("summary", "").strip().lower()
            if s:
                seen_summaries.add(s)

        useful_recent_context: List[Dict[str, Any]] = []
        for view_item, meta in context_candidates:
            summary_lower = view_item.get("summary", "").strip().lower()
            if summary_lower in seen_summaries:
                continue
            importance = str(view_item.get("importance") or "medium").strip().lower()
            if importance == "low":
                continue
            item_ts = parse_iso_utc(meta.get("last_reviewed_at"))
            if item_ts is None:
                logger.error(
                    "[%s] suppressed item '%s' has no last_reviewed_at — skipping from context. "
                    "This is a bug: all dayflow items must carry a timestamp.",
                    self.name,
                    view_item.get("item_id", "?"),
                )
                continue
            if item_ts < context_recency_cutoff:
                continue
            seen_summaries.add(summary_lower)
            useful_recent_context.append(view_item)
        useful_recent_context.sort(key=_importance_sort_key)

        self.blackboard.update_state_value("actionable_items", actionable_items)
        self.blackboard.update_state_value("artifacts_context", artifacts_context)
        self.blackboard.update_state_value("watching_upcoming", watching_upcoming)
        self.blackboard.update_state_value("recently_acted_on", recently_acted_on)
        self.blackboard.update_state_value("useful_recent_context", useful_recent_context)

        logger.info(
            "[%s] views built: important=%d artifacts=%d watching=%d acted_on=%d context=%d stale_dropped=%d",
            self.name,
            len(actionable_items),
            len(artifacts_context),
            len(watching_upcoming),
            len(recently_acted_on),
            len(useful_recent_context),
            stale_dropped,
        )
        self.blackboard.update_state_value("last_agent", self.name)
