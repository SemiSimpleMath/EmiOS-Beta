from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import resolve_short_id, write_dayflow_item
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import local_to_utc

logger = get_logger(__name__)

_ALLOWED_ITEM_STATES = {
    "new",
    "artifact",
    "needs_planning",
    "important_open",
    "actionable",
    "watching",
    "waiting",
    "acted_on",
    "suppressed",
    "closed",
}

_STATE_MOVER_FORBIDDEN_TARGETS = frozenset({"acted_on", "closed"})
_STATE_MOVER_FORBIDDEN_SOURCES = frozenset({"dispatched", "acted_on", "closed"})


def _normalize_reactivate_at(mutations: List[Dict[str, Any]]) -> None:
    """Map LLM-facing ``reactivate_at`` to internal ``reactivate_at_utc``."""
    for mut in mutations:
        if not isinstance(mut, dict):
            continue
        raw = mut.pop("reactivate_at", None)
        if raw and isinstance(raw, str) and raw.strip():
            try:
                mut["reactivate_at_utc"] = local_to_utc(raw.strip()).isoformat()
            except Exception as e:
                logger.error("Could not convert reactivate_at to UTC: %r (%s)", raw, e)
                logger.debug("reactivate_at conversion exception details", exc_info=True)
                raise


class StateTransitionGuardNode(ControlNode):
    """
    Deterministic guardrail for state mutation payload shape.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        from app.assistant.dayflow_orchestrator.contracts import validate_mutations

        parsed = self.blackboard.get_state_value("state_mutations", [])
        if parsed is None:
            parsed = []
        validate_mutations(parsed, f"{self.name}::state_mutations")
        if not isinstance(parsed, list):
            raise ValueError(f"[{self.name}] state_mutations must be a list, got {type(parsed).__name__}.")

        existing_items = get_dayflow_items()
        source_type_by_item_id: Dict[str, str] = {}
        actual_state_by_item_id: Dict[str, str] = {}
        for item in existing_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                continue
            source_type_by_item_id[item_id] = str(meta.get("source_type") or "").strip().lower()
            actual_state_by_item_id[item_id] = str(meta.get("state") or "").strip().lower()

        normalized: List[Dict[str, Any]] = []
        for idx, row in enumerate(parsed):
            if not isinstance(row, dict):
                raise ValueError(f"[{self.name}] state_mutations[{idx}] must be an object.")
            raw_id = str(row.get("item_id") or "").strip()
            item_id = resolve_short_id(raw_id)
            if item_id != raw_id:
                row = dict(row)
                row["item_id"] = item_id
            if item_id and source_type_by_item_id.get(item_id) == "plan":
                raise ValueError(
                    f"[{self.name}] state_mover attempted to mutate legacy plan step '{item_id}'. "
                    "Legacy plan steps (source_type=plan) must not be mutated by state_mover."
                )
            from_state = str(row.get("from_state") or "").strip().lower()
            to_state = str(row.get("to_state") or "").strip().lower()
            if from_state and from_state not in _ALLOWED_ITEM_STATES:
                raise ValueError(f"[{self.name}] invalid from_state='{from_state}' in mutation index {idx}.")
            if to_state and to_state not in _ALLOWED_ITEM_STATES:
                raise ValueError(f"[{self.name}] invalid to_state='{to_state}' in mutation index {idx}.")
            # Check the ACTUAL state from DB, not the LLM's claimed from_state.
            actual_state = actual_state_by_item_id.get(item_id, from_state)
            if actual_state in _STATE_MOVER_FORBIDDEN_SOURCES:
                logger.warning(
                    "[%s] state_mover tried to mutate '%s' (actual_state='%s', claimed='%s'); dropping.",
                    self.name, item_id, actual_state, from_state,
                )
                continue
            if to_state in _STATE_MOVER_FORBIDDEN_TARGETS:
                logger.warning(
                    "[%s] state_mover tried to_state='%s' for '%s'; redirecting to 'actionable'.",
                    self.name, to_state, item_id,
                )
                row = dict(row)
                row["to_state"] = "actionable"
                row["reason"] = str(row.get("reason") or "") + f" [guard: {to_state}→actionable]"
            normalized.append(row)

        # Normalize before persisting — reactivate_at must be valid before
        # the guard flag is set, otherwise downstream sees un-normalized data.
        if normalized:
            _normalize_reactivate_at(normalized)

        self.blackboard.update_state_value("state_mutations", normalized)
        self.blackboard.update_state_value("state_transition_guard_ok_tf", True)

        # ── Persist mutations immediately so downstream agents and next tick
        #    read authoritative states from the DB. ──
        if normalized:
            for mut in normalized:
                mut_item_id = str(mut.get("item_id") or "").strip()
                to_state = str(mut.get("to_state") or "").strip()
                mut_reason = str(mut.get("reason") or "").strip()
                mut_updates = {}
                # Pass through reactivate_at_utc if present.
                if "reactivate_at_utc" in mut:
                    mut_updates["reactivate_at_utc"] = mut["reactivate_at_utc"]
                write_dayflow_item(
                    mut_item_id,
                    state=to_state,
                    updates=mut_updates if mut_updates else None,
                    reason=mut_reason,
                    caller=self.name,
                )
                # Update in-memory metadata to match.
                for item in existing_items:
                    if not isinstance(item, dict):
                        continue
                    item_meta = get_meta(item)
                    if str(item_meta.get("item_id") or item.get("id") or "").strip() == mut_item_id:
                        item_meta["state"] = to_state
                        if mut_reason:
                            item_meta["state_reason"] = mut_reason
                        if "reactivate_at_utc" in mut:
                            item_meta["reactivate_at_utc"] = mut["reactivate_at_utc"]
                        break
            logger.info(
                "[%s] applied %d state mutation(s) via write_dayflow_item.",
                self.name, len(normalized),
            )

        self.blackboard.update_state_value("last_agent", self.name)
