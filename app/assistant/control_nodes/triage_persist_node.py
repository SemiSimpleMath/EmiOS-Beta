from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TriagePersistNode(ControlNode):
    """
    Persists triage decisions to DB and reloads active_dayflow_items immediately
    after triage_spawn_guard_node, so that strategic_planner reads authoritative state.

    triage_spawn_guard_node mutates item metadata in memory only (state →
    needs_planning / artifact).  Without this node those changes are invisible
    to the planner and only land in the DB at the very end of the tick, which
    means the planner operates on stale state.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        self._persist_triage_decisions(now_utc)

        self.blackboard.update_state_value("last_agent", self.name)

    def _persist_triage_decisions(self, now_utc: datetime) -> None:
        admitted: List[Dict[str, Any]] = self.blackboard.get_state_value("admitted_artifacts", []) or []
        if not isinstance(admitted, list):
            raise ValueError(
                f"[{self.name}] admitted_artifacts must be a list, got {type(admitted).__name__}."
            )
        if not admitted:
            logger.info("[%s] no admitted_artifacts to persist.", self.name)
            return

        batch_items: List[Dict[str, Any]] = []
        skipped = 0
        for item in admitted:
            if not isinstance(item, dict):
                logger.warning("[%s] skipping non-dict admitted artifact: %s", self.name, type(item).__name__)
                skipped += 1
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                logger.warning("[%s] skipping admitted artifact with no item_id", self.name)
                skipped += 1
                continue

            meta["last_reviewed_at"] = now_utc.isoformat()
            item_dict = dict(meta)
            item_dict.setdefault("item_id", item_id)
            item_dict.setdefault("source_type", str(meta.get("source_type") or "triage_admitted"))
            item_dict.setdefault("summary", str(meta.get("summary") or item.get("content") or ""))
            batch_items.append(item_dict)

        write_dayflow_items_batch(batch_items, caller=self.name)
        logger.info("[%s] persisted %d triage decision(s) to DB.", self.name, len(batch_items))

