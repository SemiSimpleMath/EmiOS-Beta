from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item, write_dayflow_items_batch
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TriagePersistNode(ControlNode):
    """
    Persists triage decisions to DB and reloads active_dayflow_items immediately
    after triage_spawn_guard_node, so that strategic_planner reads authoritative state.

    triage_spawn_guard_node mutates item metadata in memory only (state →
    artifact for ADMIT, suppressed for REJECT). Without this node those changes
    are invisible to the planner and only land in the DB at the very end of
    the tick, which means the planner operates on stale state.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        self._persist_admitted(now_utc)
        self._persist_rejected(now_utc)

        self.blackboard.update_state_value("last_agent", self.name)

    def _persist_admitted(self, now_utc: datetime) -> None:
        admitted: List[Dict[str, Any]] = self.blackboard.get_state_value("admitted_artifacts", []) or []
        if not isinstance(admitted, list):
            raise ValueError(
                f"[{self.name}] admitted_artifacts must be a list, got {type(admitted).__name__}."
            )
        if not admitted:
            logger.info("[%s] no admitted_artifacts to persist.", self.name)
            return

        batch_items: List[Dict[str, Any]] = []
        for item in admitted:
            if not isinstance(item, dict):
                logger.warning("[%s] skipping non-dict admitted artifact: %s", self.name, type(item).__name__)
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                logger.warning("[%s] skipping admitted artifact with no item_id", self.name)
                continue

            meta["last_reviewed_at"] = now_utc.isoformat()
            item_dict = dict(meta)
            item_dict.setdefault("item_id", item_id)
            item_dict.setdefault("source_type", str(meta.get("source_type") or "triage_admitted"))
            item_dict.setdefault("summary", str(meta.get("summary") or item.get("content") or ""))
            batch_items.append(item_dict)

        write_dayflow_items_batch(batch_items, caller=self.name)
        logger.info("[%s] persisted %d admitted triage decision(s) to DB.", self.name, len(batch_items))

    def _persist_rejected(self, now_utc: datetime) -> None:
        rejected: List[Dict[str, Any]] = self.blackboard.get_state_value("rejected_artifacts", []) or []
        if not isinstance(rejected, list):
            raise ValueError(
                f"[{self.name}] rejected_artifacts must be a list, got {type(rejected).__name__}."
            )
        if not rejected:
            return

        persisted = 0
        for item in rejected:
            if not isinstance(item, dict):
                logger.warning("[%s] skipping non-dict rejected artifact: %s", self.name, type(item).__name__)
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                logger.warning("[%s] skipping rejected artifact with no item_id", self.name)
                continue
            reason = str(meta.get("state_reason") or "triage_reject").strip()
            write_dayflow_item(
                item_id,
                state="suppressed",
                reason=reason,
                caller=self.name,
            )
            persisted += 1
        logger.info("[%s] persisted %d rejected triage decision(s) to DB.", self.name, persisted)

