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
        # Scope to dayflow tickets at the DB layer — the formatted dict from
        # get_responded_tickets_categorized() does not carry ticket_type, so a
        # downstream filter on it would silently drop everything (and did).
        responded_tickets = get_responded_tickets_categorized(
            since_utc=now_utc - timedelta(hours=2),
            ticket_type="dayflow_orchestrator",
        )

        self.blackboard.update_state_value("active_dayflow_items", active_items)
        self.blackboard.update_state_value("active_plan_synopses", plan_synopses)
        self.blackboard.update_state_value("recent_dayflow_chat_history", chat_history)
        self.blackboard.update_state_value("recent_responded_tickets", responded_tickets)

        # Work-object nodes parked on an external event — the state_mover wakes these (it replaces the
        # standalone event_waker). The graph handles their time/dependency gates; only the event match is
        # the state_mover's job.
        try:
            self._build_work_object_waits(all_items)
        except Exception as e:
            logger.error("[%s] work-object waits build failed: %s", self.name, e)
            logger.debug("[%s] work-object waits exception", self.name, exc_info=True)

        # Ready work nodes (gates clear) — the state_mover promotes these to actionable, or holds the few
        # that shouldn't reach the user right now (quiet hours, meeting, away). Default is promote.
        try:
            self._build_promotion_candidates()
        except Exception as e:
            logger.error("[%s] promotion candidates build failed: %s", self.name, e)
            logger.debug("[%s] promotion candidates exception", self.name, exc_info=True)

        logger.info(
            "[%s] prepared: active=%d synopses=%d chat=%d",
            self.name,
            len(active_items),
            len(plan_synopses),
            len(chat_history),
        )
        self.blackboard.update_state_value("last_agent", self.name)

    def _build_promotion_candidates(self):
        """Ready work-object nodes (gates clear: status proposed/waiting, time + deps met, NOT parked on an
        external event) — the nodes the state_mover may promote to `actionable` this tick, or HOLD for a
        better moment. Same filter the persist node's promotion uses, so what the LLM sees == what gets
        promoted/held. Sets ``ready_work_nodes`` on the blackboard (rendered like waiting_work_nodes)."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from work_objects.model import utcnow
        from work_objects.store import FAMILY_BY_TYPE, TRANSITIONS

        store = get_dayflow_work_store()
        now = utcnow()
        ready: List[Dict[str, Any]] = []
        for s in store.list_work_objects():
            if str(s.get("status") or "").lower() in {"done", "abandoned"}:
                continue
            wo = store.load(s["id"])
            goal_id = wo.goal_node_id
            for n in wo.nodes.values():
                if n.id == goal_id or n.status not in {"proposed", "waiting"}:
                    continue
                if str(getattr(n, "wake_kind", None) or "") in {"event", "signal"}:
                    continue   # external waits are shown in WORK-OBJECT WAITS instead
                # user_reply asks appear here once DUE: an in-flight ask has wake_at (its re-ask time) in
                # the future so is_ready holds it out; a due re-ask — or a repair-escalated ask with no
                # wake_at awaiting its first surface — is promotable, and the state_mover may HOLD it like
                # any other ready node (quiet hours, meetings, away).
                if not wo.is_ready(n, now):
                    continue
                family = FAMILY_BY_TYPE.get(n.type, "spine")
                if "actionable" not in TRANSITIONS.get(family, {}).get(n.status, set()):
                    continue   # non-dispatchable family
                ready.append({
                    "task_id": f"{s['id']}::{n.id}",
                    "context": (n.title or n.content or "").strip().replace("\n", " ")[:140],
                    "kind": str(getattr(n, "type", "") or ""),
                })
        self.blackboard.update_state_value("ready_work_nodes", ready)

    def _build_work_object_waits(self, all_items):
        """Find work-object nodes parked on an EXTERNAL event (deps + time already met, so is_ready is
        True) and the recent incoming intake to match them against. Sets waiting_work_nodes +
        work_wait_intake on the blackboard. deps/time are the graph's job; only the event match is the
        state_mover's (this replaces the standalone event_waker)."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from work_objects.model import utcnow

        store = get_dayflow_work_store()
        now = utcnow()
        waiting: List[Dict[str, Any]] = []
        for s in store.list_work_objects():
            if str(s.get("status") or "").lower() in {"done", "abandoned"}:
                continue
            wo = store.load(s["id"])
            for n in wo.nodes.values():
                # user_reply is an in-flight ask — the dispatch surfaces it and re-asks until the user
                # replies, and the reply is then recorded as the node's result. The state_mover only wakes
                # passive EXTERNAL waits (event/signal) by matching incoming intake.
                if getattr(n, "wake_kind", None) in {"event", "signal"} and wo.is_ready(n, now):
                    waiting.append({
                        "task_id": f"{s['id']}::{n.id}",
                        "waiting_for": getattr(n, "wake_ref", "") or "",
                        "context": (n.title or n.content or "").strip().replace("\n", " ")[:140],
                    })
        self.blackboard.update_state_value("waiting_work_nodes", waiting)
        if not waiting:
            return

        intake: List[str] = []
        for item in all_items:
            meta = get_meta(item)
            st = str(meta.get("source_type") or "").strip().lower()
            if st == "email":
                summ = meta.get("email_summary", "")
                intake.append(f"Email from {meta.get('email_sender', 'Unknown')}: "
                              f"\"{meta.get('email_subject', meta.get('summary', ''))}\""
                              + (f" — {summ}" if summ else ""))
            elif st == "chat":
                summ = str(meta.get("summary") or "").strip()
                if summ:
                    intake.append(f"Chat: {summ}")
        self.blackboard.update_state_value("work_wait_intake", intake[:30])
