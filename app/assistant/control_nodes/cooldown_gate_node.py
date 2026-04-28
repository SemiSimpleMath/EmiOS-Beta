from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import get_max_short_id
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc_strict

logger = get_logger(__name__)


class CooldownGateNode(ControlNode):
    """
    Deterministic anti-dup cooldown filter.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        from app.assistant.dayflow_orchestrator.contracts import validate_dayflow_items

        items = self.blackboard.get_state_value("intake_items_deduped", [])
        if items is None:
            items = []
        validate_dayflow_items(items, f"{self.name}::intake_items_deduped")
        if not isinstance(items, list):
            raise ValueError(f"[{self.name}] intake_items_deduped must be a list, got {type(items).__name__}.")

        now_utc = datetime.now(timezone.utc)
        eligible: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []
        auto_admitted: List[Dict[str, Any]] = []

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"[{self.name}] intake_items_deduped[{idx}] must be an object.")
            meta = get_meta(item)

            source_type = str(meta.get("source_type") or "").strip().lower()
            if source_type == "calendar":
                meta["state"] = "artifact"
                meta["state_reason"] = "auto_admitted_calendar"
                auto_admitted.append(item)
                continue

            raw_cooldown = meta.get("cooldown_until")
            cooldown_until = parse_iso_utc_strict(raw_cooldown, label=f"cooldown_until[{idx}]") if raw_cooldown else None
            if cooldown_until is not None and now_utc < cooldown_until:
                blocked.append(item)
                continue
            eligible.append(item)

        from app.assistant.dayflow_orchestrator.contracts import assign_short_ids

        max_existing = get_max_short_id()
        all_incoming = eligible + auto_admitted
        assign_short_ids(all_incoming, counter_start=max_existing + 1)

        self.blackboard.update_state_value("eligible_items_now", eligible)
        self.blackboard.update_state_value("cooldown_blocked_items", blocked)
        self.blackboard.update_state_value("auto_admitted_artifacts", auto_admitted)
        logger.info(
            "[%s] cooldown filter: %d eligible, %d blocked, %d calendar auto-admitted.",
            self.name,
            len(eligible),
            len(blocked),
            len(auto_admitted),
        )

        if not eligible:
            logger.info("[%s] no eligible items for triage — skipping intake_triage.", self.name)
            self.blackboard.update_state_value("next_agent", "triage_spawn_guard_node")

        self.blackboard.update_state_value("last_agent", self.name)
