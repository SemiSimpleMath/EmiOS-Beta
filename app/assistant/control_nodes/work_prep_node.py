"""The planning tick's first stage: what every later stage reads, prepared once.

- Pending concern and belief feedback is delivered (receipts a crash left undelivered; concern_feedback,
  belief_engine/work_feedback).
- The reminders the fixed scheduler sent recently (`scheduled_reminder_history`), read by the architect and the
  state mover.
Then the tick goes on to the brain's list (brain_list_intake_node).

Until 2026-10-10 this was the steward's prep node (strategic_planner_wo_prep_node), and intake triage ran before it.
The steward (strategic_planner_wo, dayflow's evaluator) decided what work exists from the intake triage admitted;
since 2026-10-06 the brain decides, through its list, and the steward was skipped (configs/brain_lanes.json, removed
with it). Owner, 2026-10-10: "retire the old steward", "triage too".
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class WorkPrepNode(ControlNode):
    def action_handler(self, message):
        from datetime import datetime, timezone

        from app.assistant.dayflow_orchestrator.communication_context import scheduled_reminder_history
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from app.assistant.subconscious.concern_feedback import recover_pending_concern_feedback
        from belief_engine.work_feedback import recover_pending_belief_feedback

        self.blackboard.update_state_value("next_agent", None)
        store = get_dayflow_work_store()
        recover_pending_concern_feedback(store)
        recover_pending_belief_feedback(store)
        self.blackboard.update_state_value("scheduled_reminder_history",
                                           scheduled_reminder_history(datetime.now(timezone.utc)))
        logger.info("[%s] prepared; on to the brain's list", self.name)
        self.blackboard.update_state_value("last_agent", self.name)
