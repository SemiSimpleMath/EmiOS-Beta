from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dispatch_sweeper import list_active_dispatches
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc_strict

logger = get_logger(__name__)


def _normalized_signal_set(items: List[Dict[str, Any]]) -> Set[str]:
    signals: Set[str] = set()
    for row in items:
        meta = get_meta(row)
        source = str(meta.get("source_type") or "").strip().lower()
        event_type = str(meta.get("event_type") or "").strip().lower()
        item_id = str(meta.get("item_id") or "").strip().lower()
        if source:
            signals.add(source)
            signals.add(f"source:{source}")
        if event_type:
            signals.add(event_type)
            signals.add(f"event:{event_type}")
        if item_id:
            signals.add(item_id)
            signals.add(f"item:{item_id}")
    return signals


class WaitInterruptPromoterNode(ControlNode):
    """
    Promote waiting/watching items into actionable_items when:
    - timer trigger is due (reactivate_at_utc), or
    - interrupt signal matches wake_signals, or
    - all blocked_by dependencies are resolved (acted_on / closed).
    """

    def _build_resolved_ids(self) -> Set[str]:
        """
        Collect item_ids whose state is terminal-ish (acted_on, closed, suppressed)
        from existing_dayflow_items + recently_acted_on views. These are considered
        resolved for the purpose of blocked_by dependency release.
        """
        resolved: Set[str] = set()
        from app.assistant.dayflow_orchestrator.state_store import RESOLVED_STATES
        resolved_states = RESOLVED_STATES

        existing = get_dayflow_items()
        for ex in existing:
            if not isinstance(ex, dict):
                continue
            meta = get_meta(ex)
            ex_state = str(meta.get("state") or "").strip().lower()
            ex_id = str(meta.get("item_id") or ex.get("id") or "").strip()
            if ex_id and ex_state in resolved_states:
                resolved.add(ex_id)

        recently_acted = self.blackboard.get_state_value("recently_acted_on", [])
        if isinstance(recently_acted, list):
            for ra in recently_acted:
                if not isinstance(ra, dict):
                    continue
                ra_id = str(ra.get("item_id") or "").strip()
                if ra_id:
                    resolved.add(ra_id)

        return resolved

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        from app.assistant.dayflow_orchestrator.contracts import (
            validate_dayflow_items,
            validate_view_items,
        )

        actionable_items = self.blackboard.get_state_value("actionable_items", [])
        watching_upcoming = self.blackboard.get_state_value("watching_upcoming", [])
        intake_items = self.blackboard.get_state_value("intake_items_deduped", [])
        if actionable_items is None:
            actionable_items = []
        if watching_upcoming is None:
            watching_upcoming = []
        if intake_items is None:
            intake_items = []
        if not isinstance(actionable_items, list):
            raise ValueError(f"[{self.name}] actionable_items must be a list.")
        if not isinstance(watching_upcoming, list):
            raise ValueError(f"[{self.name}] watching_upcoming must be a list.")
        if not isinstance(intake_items, list):
            raise ValueError(f"[{self.name}] intake_items_deduped must be a list.")

        validate_view_items(actionable_items, f"{self.name}::actionable_items")
        validate_view_items(watching_upcoming, f"{self.name}::watching_upcoming")
        validate_dayflow_items(intake_items, f"{self.name}::intake_items_deduped")

        now_utc = datetime.now(timezone.utc)
        signal_set = _normalized_signal_set([x for x in intake_items if isinstance(x, dict)])
        resolved_ids = self._build_resolved_ids()
        promoted: List[Dict[str, Any]] = []
        remaining_watch: List[Dict[str, Any]] = []
        important_ids = {str(x.get("item_id") or "").strip() for x in actionable_items if isinstance(x, dict)}

        for idx, item in enumerate(watching_upcoming):
            if not isinstance(item, dict):
                raise ValueError(f"[{self.name}] watching_upcoming[{idx}] must be an object.")
            state = str(item.get("state") or "").strip().lower()
            item_id = str(item.get("item_id") or "").strip()
            raw_reactivate = item.get("reactivate_at_utc")
            reactivate_at_utc = parse_iso_utc_strict(raw_reactivate, label=f"reactivate_at_utc[{item_id}]") if raw_reactivate else None
            wake_signals = item.get("wake_signals")
            wake_list: List[str] = []
            if isinstance(wake_signals, list):
                wake_list = [str(v).strip().lower() for v in wake_signals if isinstance(v, str) and str(v).strip()]
            elif wake_signals not in (None, ""):
                raise ValueError(f"[{self.name}] wake_signals for item '{item_id}' must be a list of strings.")

            blocked_by = item.get("blocked_by")
            blocked_list: List[str] = []
            if isinstance(blocked_by, list):
                blocked_list = [str(v).strip() for v in blocked_by if isinstance(v, str) and str(v).strip()]

            timer_due = reactivate_at_utc is not None and now_utc >= reactivate_at_utc
            signaled = bool(wake_list) and any(sig in signal_set for sig in wake_list)
            deps_resolved = bool(blocked_list) and all(dep in resolved_ids for dep in blocked_list)

            promotable = state in {"waiting", "watching"}
            if promotable and (timer_due or signaled or deps_resolved):
                promoted_item = dict(item)
                promoted_item["state"] = "actionable"
                promoted_item["importance"] = str(
                    promoted_item.get("priority_on_wake")
                    or promoted_item.get("importance")
                    or "high"
                )
                if deps_resolved and not timer_due and not signaled:
                    promoted_item["state_reason"] = f"{state}_dependency_released"
                elif timer_due:
                    promoted_item["state_reason"] = f"{state}_reactivated_by_timer"
                else:
                    promoted_item["state_reason"] = f"{state}_reactivated_by_signal"

                if not item_id or item_id not in important_ids:
                    promoted.append(promoted_item)
                    if item_id:
                        important_ids.add(item_id)
                continue
            remaining_watch.append(item)

        merged_important = [x for x in actionable_items if isinstance(x, dict)] + promoted

        # Filter out actionable items that already have an active dispatch.
        # Read dispatches directly from the DB at the point of use rather
        # than carrying them in across the manager boundary.
        dispatched_item_ids: set[str] = set()
        for d in list_active_dispatches():
            acted_id = str(d.get("acted_on_item_id") or "").strip()
            if acted_id:
                dispatched_item_ids.add(acted_id)
        if dispatched_item_ids:
            before = len(merged_important)
            merged_important = [
                x for x in merged_important
                if str(x.get("item_id") or "").strip() not in dispatched_item_ids
            ]
            filtered = before - len(merged_important)
            if filtered:
                logger.info(
                    "[%s] filtered %d actionable item(s) already covered by active dispatches.",
                    self.name, filtered,
                )

        self.blackboard.update_state_value("actionable_items", merged_important)
        self.blackboard.update_state_value("watching_upcoming", remaining_watch)
        self.blackboard.update_state_value("wait_promoted_items", promoted)
        self.blackboard.update_state_value("wait_interrupt_signal_count", len(signal_set))

        if not merged_important:
            logger.info("[%s] no actionable items — skipping action_selector.", self.name)
            self.blackboard.update_state_value("next_agent", "post_room_finalize_node")

        self.blackboard.update_state_value("last_agent", self.name)
