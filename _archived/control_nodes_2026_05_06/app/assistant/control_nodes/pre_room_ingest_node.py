from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc_strict

logger = get_logger(__name__)

_ARTIFACT_TTL_HOURS = 24
_NEEDS_PLANNING_TTL_HOURS = 12


class PreRoomIngestNode(ControlNode):
    """
    Deterministic pre-room normalization hook.

    1) Sweeps stale artifact and needs_planning items to closed.
    2) Seeds ingestion keys used by intake/state agents and validates core payload shape.
    """

    def _sweep_stale_items(self, now_utc: datetime) -> int:
        existing = get_dayflow_items()
        if not existing:
            return 0

        artifact_cutoff = now_utc - timedelta(hours=_ARTIFACT_TTL_HOURS)
        needs_planning_cutoff = now_utc - timedelta(hours=_NEEDS_PLANNING_TTL_HOURS)

        stale_mutations: List[Dict[str, Any]] = []

        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            state = str(meta.get("state") or "").strip().lower()
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                continue

            raw_reviewed = meta.get("last_reviewed_at")
            if not raw_reviewed:
                continue
            last_reviewed = parse_iso_utc_strict(raw_reviewed, label=f"last_reviewed_at[{item_id}]")

            if state == "artifact" and last_reviewed < artifact_cutoff:
                stale_mutations.append({
                    "item_id": item_id,
                    "from_state": "artifact",
                    "to_state": "closed",
                    "reason": f"stale_sweep_artifact_{_ARTIFACT_TTL_HOURS}h",
                })
            elif state == "needs_planning" and last_reviewed < needs_planning_cutoff:
                stale_mutations.append({
                    "item_id": item_id,
                    "from_state": "needs_planning",
                    "to_state": "closed",
                    "reason": f"stale_sweep_needs_planning_{_NEEDS_PLANNING_TTL_HOURS}h",
                })

        if not stale_mutations:
            return 0

        for mut in stale_mutations:
            write_dayflow_item(
                mut["item_id"],
                state=mut["to_state"],
                reason=mut["reason"],
                caller=self.name,
            )

        logger.info(
            "[%s] stale sweep: closed %d item(s) (artifact>%dh=%d, needs_planning>%dh=%d).",
            self.name,
            len(stale_mutations),
            _ARTIFACT_TTL_HOURS,
            sum(1 for m in stale_mutations if m["from_state"] == "artifact"),
            _NEEDS_PLANNING_TTL_HOURS,
            sum(1 for m in stale_mutations if m["from_state"] == "needs_planning"),
        )
        return len(stale_mutations)

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        # Planning mode bypass: skip the full ingest pipeline and route
        # directly to the plan_mode conversation agent.
        room_mode = str(self.blackboard.get_state_value("room_mode", "") or "").strip().lower()
        if room_mode == "planning_mode":
            logger.info("[%s] room_mode=planning_mode — bypassing ingest, routing to plan_mode.", self.name)
            self.blackboard.update_state_value("next_agent", "dayflow_orchestrator::plan_mode")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        now_utc = datetime.now(timezone.utc)

        swept = self._sweep_stale_items(now_utc)
        self.blackboard.update_state_value("pre_room_stale_swept_count", swept)

        from app.assistant.dayflow_orchestrator.contracts import validate_dayflow_items
        incoming = self.blackboard.get_state_value("incoming_items_new", [])
        if incoming is None:
            incoming = []
        validate_dayflow_items(incoming, f"{self.name}::incoming_items_new")
        if not isinstance(incoming, list):
            raise ValueError(f"[{self.name}] incoming_items_new must be a list, got {type(incoming).__name__}.")

        normalized: List[Dict[str, Any]] = []
        for idx, item in enumerate(incoming):
            if not isinstance(item, dict):
                raise ValueError(
                    f"[{self.name}] incoming_items_new[{idx}] must be an object, got {type(item).__name__}."
                )
            normalized.append(deepcopy(item))

        self.blackboard.update_state_value("intake_items_new", normalized)
        self.blackboard.update_state_value("intake_items_new_count", len(normalized))
        self.blackboard.update_state_value("pre_room_processed_tf", True)
        logger.info("[%s] seeded %d intake item(s), swept %d stale.", self.name, len(normalized), swept)
        self.blackboard.update_state_value("last_agent", self.name)
