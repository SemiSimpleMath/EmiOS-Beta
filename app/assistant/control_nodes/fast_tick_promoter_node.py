"""Fast-tick deterministic promoter.

Runs only on fast-tick paths (scheduler woke for a specific due item timer).
Promotes one named item from ``waiting`` → ``actionable`` via write_dayflow_item,
which validates the transition against ALLOWED_TRANSITIONS and stamps
``last_reviewed_at``. Acts as the atomic claim: if the item is no longer in
``waiting`` (already dispatched, closed, etc.), the writer raises and we exit
cleanly without dispatching.

Downstream the pipeline continues: view_materializer rebuilds buckets from the
freshly-updated DB, action_selector picks the now-actionable item, switchboard
dispatches it through the normal provenance machinery.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
from app.assistant.dayflow_orchestrator.state_store import get_latest_dayflow_item_by_id
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Fast-tick only wakes items currently waiting (or actively watching). A scheduled
# timer firing should NOT auto-reopen a closed item — even though
# ALLOWED_TRANSITIONS permits closed → actionable (manual reopen), an in-flight
# scheduler job from before the user/agent closed the item should NOT silently
# revive it.
_FAST_TICK_PROMOTABLE_STATES = frozenset({"waiting", "watching"})


class FastTickPromoterNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        item_id = str(self.blackboard.get_state_value("triggered_item_id") or "").strip()
        if not item_id:
            logger.error(
                "[%s] fast-tick path entered with no triggered_item_id — exiting cleanly.",
                self.name,
            )
            self.blackboard.update_state_value("next_agent", "manager_exit_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        current = get_latest_dayflow_item_by_id(item_id, include_terminal=True)
        if current is None:
            logger.warning(
                "[%s] triggered_item_id=%s not found in DB — exiting.",
                self.name, item_id,
            )
            self.blackboard.update_state_value("next_agent", "manager_exit_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        current_state = str(get_meta(current).get("state") or "").strip().lower()
        if current_state not in _FAST_TICK_PROMOTABLE_STATES:
            # Item moved out of waiting since the timer was scheduled (user
            # closed it, agent already dispatched it, etc.). Honor the newer
            # state — do not auto-revive.
            logger.info(
                "[%s] item %s is now state=%s (not waiting/watching); skipping fast-tick promotion.",
                self.name, item_id, current_state,
            )
            self.blackboard.update_state_value("next_agent", "manager_exit_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        try:
            write_dayflow_item(
                item_id,
                state="actionable",
                reason="scheduled_timer_fired",
                caller=self.name,
            )
            logger.info(
                "[%s] promoted item %s (state=%s) to actionable (scheduled timer fired).",
                self.name, item_id, current_state,
            )
        except ValueError as e:
            # Defensive: state machine refused the transition (race between
            # the state-check above and the write).
            logger.warning(
                "[%s] fast-tick promotion declined for %s: %s",
                self.name, item_id, e,
            )
            self.blackboard.update_state_value("next_agent", "manager_exit_node")

        self.blackboard.update_state_value("last_agent", self.name)
