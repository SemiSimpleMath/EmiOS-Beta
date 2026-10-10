"""The list-item intake node: work made from the brain's list, in the planning tick (2026-10-05).

Runs between the tick's prep node and the architect (multi_agents/dayflow_orchestrator_manager/config.yaml).
The logic is dayflow_orchestrator/brain_list_intake.py: new shells for entries the brain decided needed, revisions
for changed entries, and the end of the work of cancelled ones (binding, 2026-10-10).
The architect then lays the new shells' graphs (they are work objects without tasks) and re-plans the revised ones
from the re-plan instruction each carries (constraints.architect_instructions), licensed as the brain's directive. A
failure here is raised: a tick that cannot read the brain's list must not plan as if it were empty.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class BrainListIntakeNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        from app.assistant.dayflow_orchestrator.brain_list_intake import deliver
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        result = deliver(get_dayflow_work_store())
        self.blackboard.update_state_value("brain_list_intake_result", result)
        self.blackboard.update_state_value("last_agent", self.name)
