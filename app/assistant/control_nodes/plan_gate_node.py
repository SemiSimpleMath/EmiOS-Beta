from __future__ import annotations

from typing import Any, Dict, List, Set

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

from app.assistant.dayflow_orchestrator.state_store import RESOLVED_STATES
_RESOLVED_STATES = RESOLVED_STATES
_PLANNER_AGENT = "dayflow_orchestrator::strategic_planner"


class PlanGateNode(ControlNode):
    """
    Compiler-style gate deciding whether strategic_planner should run.

    The planner is intentionally *not* an always-on runtime brain. It should run
    only when new planning work is requested (``needs_planning``) or when an
    explicit force flag is set. Day-to-day progression is handled by
    state_mover + action_selector.
    """

    def _collect_resolved_ids(self, existing: List[Dict[str, Any]]) -> Set[str]:
        resolved: Set[str] = set()
        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            state = str(meta.get("state") or "").strip().lower()
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id and state in _RESOLVED_STATES:
                resolved.add(item_id)
        return resolved

    def _has_plan_progression(
        self, existing: List[Dict[str, Any]], resolved_ids: Set[str]
    ) -> bool:
        """True when a waiting plan step has all blocked_by deps resolved."""
        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            plan_id = str(meta.get("plan_id") or "").strip()
            if not plan_id:
                continue
            state = str(meta.get("state") or "").strip().lower()
            if state != "waiting":
                continue
            blocked_by = meta.get("blocked_by")
            if not isinstance(blocked_by, list) or not blocked_by:
                continue
            all_resolved = all(
                str(dep).strip() in resolved_ids
                for dep in blocked_by
                if isinstance(dep, str) and str(dep).strip()
            )
            if all_resolved:
                logger.info(
                    "[%s] plan progression: step '%s' (plan=%s) deps resolved.",
                    self.name,
                    meta.get("item_id", ""),
                    plan_id,
                )
                return True
        return False

    def _has_unreviewed_execution_results(
        self, existing: List[Dict[str, Any]]
    ) -> bool:
        """True when a plan step has execution_result the planner hasn't seen."""
        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            if str(meta.get("source_type") or "").strip() != "plan":
                continue
            if not meta.get("execution_result"):
                continue
            if meta.get("planner_reviewed"):
                continue
            logger.info(
                "[%s] unreviewed execution result on step '%s' (plan=%s).",
                self.name,
                meta.get("item_id", ""),
                meta.get("plan_id", ""),
            )
            return True
        return False

    def _has_active_plans(self, existing: List[Dict[str, Any]]) -> bool:
        """True when any non-terminal plan step exists (periodic validation)."""
        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            if str(meta.get("source_type") or "").strip() != "plan":
                continue
            state = str(meta.get("state") or "").strip().lower()
            if state not in _RESOLVED_STATES:
                return True
        return False

    def _has_planning_flags(self, existing: List[Dict[str, Any]]) -> bool:
        """True when any source flagged something for planning.

        Checks current-run outputs (spawn_items, state_spawned_items,
        state_mutations) **and** existing DB items that carry
        ``needs_planning=True`` without an assigned ``plan_id``.
        """
        for key in ("spawn_items", "state_spawned_items"):
            spawns = self.blackboard.get_state_value(key, [])
            if not isinstance(spawns, list):
                continue
            for item in spawns:
                if not isinstance(item, dict):
                    continue
                if item.get("needs_planning"):
                    logger.info(
                        "[%s] planning flag in %s item '%s'.",
                        self.name,
                        key,
                        item.get("item_id", ""),
                    )
                    return True

        mutations = self.blackboard.get_state_value("state_mutations", []) or []
        if isinstance(mutations, list):
            for mut in mutations:
                if isinstance(mut, dict) and mut.get("needs_planning"):
                    logger.info("[%s] planning flag in state_mutation.", self.name)
                    return True

        for item in existing:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            if not meta.get("needs_planning"):
                continue
            if meta.get("plan_id"):
                continue
            state = str(meta.get("state") or "").strip().lower()
            if state in _RESOLVED_STATES:
                continue
            logger.info(
                "[%s] planning flag on existing item '%s' (state=%s).",
                self.name,
                meta.get("item_id", ""),
                state,
            )
            return True

        return False

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        from app.assistant.dayflow_orchestrator.contracts import (
            validate_dayflow_items,
            validate_spawn_items,
        )

        existing = get_dayflow_items()
        validate_dayflow_items(existing, f"{self.name}::existing_dayflow_items")
        validate_spawn_items(
            self.blackboard.get_state_value("spawn_items", []),
            f"{self.name}::spawn_items",
        )
        validate_spawn_items(
            self.blackboard.get_state_value("state_spawned_items", []),
            f"{self.name}::state_spawned_items",
        )

        # Triage-planner mode: intake_triage owns planning and grouping directly.
        # Keep this gate deterministic and explicit so strategic_planner is not invoked.
        self.blackboard.update_state_value("plan_gate_triggered", False)
        self.blackboard.update_state_value("plan_gate_reason", "triage_planner_mode")
        logger.info("[%s] strategic planner disabled; triage_planner_mode active.", self.name)

        self.blackboard.update_state_value("last_agent", self.name)
