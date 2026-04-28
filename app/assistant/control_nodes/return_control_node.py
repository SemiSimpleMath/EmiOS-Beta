from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.control_nodes.control_node import ControlNode

logger = get_logger(__name__)

class ReturnControlNode(ControlNode):
    def __init__(self, name, blackboard, agent_registry, tool_registry):
        super().__init__(name, blackboard, agent_registry, tool_registry)

    def action_handler(self, message: Message):
        """
        Acts as a signaling mechanism for an agent that has finished its task.

        It determines if the agent is a sub-task (and sets up for returning control)
        or a top-level task (and sets up for exiting the manager).
        It does NOT pop the call stack; that is the ToolResultHandler's job.
        """
        logger.info(f"[{self.name}] An agent has signaled it is finished.")

        # Capture the planner's last note as final_answer_content so the
        # final_answer agent can see extracted data (headlines, etc.).
        note = str(self.blackboard.get_state_value("note", "") or "").strip()
        if note:
            self.blackboard.append_state_value("final_answer_content", note)

        # Peek at the call stack to see if we are in a sub-task.
        current_context = self.blackboard.get_current_call_context()

        if current_context:
            # Case 2: We are in a sub-task. The agent was called by another agent.
            calling_agent = current_context[0]  # (caller, callee, scope_id)

            # Create a personalized exit state for the delegator to interpret.
            # e.g., "PlannerA_exit"
            personalized_exit_name = f"{calling_agent}_exit"

            logger.info(
                f"[{self.name}] Sub-task finished. Setting last_agent to '{personalized_exit_name}'. "
                f"The delegator should now route to the ToolResultHandler."
            )
            # The delegator will see this state and know to activate the ToolResultHandler,
            # which will then properly process the result and pop the scope.
            self.blackboard.update_state_value('last_agent', personalized_exit_name)
        else:
            # Case 1: This is a top-level agent (e.g., a master planner). There's no scope to pop.
            logger.warning(
                f"[{self.name}] A top-level agent has finished. Signaling for manager exit."
            )
            # This global flag tells the MultiAgentManager to stop its execution loop.
            self.blackboard.update_global_state_value("exit", True)
