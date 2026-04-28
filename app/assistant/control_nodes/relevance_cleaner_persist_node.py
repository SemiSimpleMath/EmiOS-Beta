"""
Persist node for the relevance cleaner agent's decisions.

Reads cleanup decisions from the blackboard, resolves short_ids to
canonical item_ids, and applies close/suppress mutations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.dayflow_item_writer import resolve_short_id, write_dayflow_item
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_LAST_RUN_KEY = "relevance_cleaner_last_run_utc"

_ACTION_TO_STATE = {
    "close": "closed",
    "suppress": "suppressed",
}


class RelevanceCleanerPersistNode(ControlNode):
    """
    Apply cleanup decisions from the relevance_cleaner agent.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        # Mark that the cleaner ran this tick.
        self.blackboard.update_state_value(_LAST_RUN_KEY, now_utc.isoformat())

        # Key matches AgentForm.decisions — scoped to the current room tick.
        decisions = self.blackboard.get_state_value("decisions", []) or []
        if not isinstance(decisions, list) or not decisions:
            logger.info("[%s] No cleanup decisions to apply.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        mutations: List[Dict[str, Any]] = []
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            raw_id = str(decision.get("item_id") or "").strip()
            action = str(decision.get("action") or "").strip().lower()
            reason = str(decision.get("reason") or "").strip()

            if not raw_id:
                logger.warning("[%s] decision missing item_id, skipping.", self.name)
                continue
            if action not in _ACTION_TO_STATE:
                logger.warning("[%s] unknown action '%s' for item '%s', skipping.", self.name, action, raw_id)
                continue

            # Resolve short_id → real item_id.
            item_id = resolve_short_id(raw_id)

            mutations.append({
                "item_id": item_id,
                "from_state": "",
                "to_state": _ACTION_TO_STATE[action],
                "reason": f"relevance_cleaner: {reason}",
            })

        if not mutations:
            logger.info("[%s] No valid mutations after filtering.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        applied = 0
        for mut in mutations:
            try:
                write_dayflow_item(
                    mut["item_id"],
                    state=mut["to_state"],
                    reason=mut["reason"],
                    caller=self.name,
                )
                applied += 1
            except ValueError as e:
                # Common case: item already in a resolved state and the cleaner
                # is asking to move it to another resolved state. The transition
                # map disallows self-loops and disallows moves out of suppressed,
                # so the writer raises. We surface as ERROR so genuine state-
                # machine violations (the over-broad part of this catch) are
                # visible in logs.
                logger.error(
                    "[%s] mutation rejected for %s (state-machine violation or "
                    "redundant target): %s",
                    self.name, mut["item_id"], e, exc_info=True,
                )

        closed = sum(1 for m in mutations if m["to_state"] == "closed")
        suppressed = sum(1 for m in mutations if m["to_state"] == "suppressed")

        logger.info(
            "[%s] Applied %d mutation(s) — closed=%d, suppressed=%d.",
            self.name, len(mutations), closed, suppressed,
        )
        self.blackboard.update_state_value("last_agent", self.name)
