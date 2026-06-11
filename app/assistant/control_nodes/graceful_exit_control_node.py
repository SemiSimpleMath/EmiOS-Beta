from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class GracefulExitControlNode(ControlNode):
    """
    Terminal routing node for the manager's graceful-exit loop.

    Reached when the main agent loop returned something other than "success"
    (max_cycles, error, unknown). The manager at this point has already given
    up — our job is to write a *deterministic, factual* abort report into
    `final_answer` so the downstream ToolResult describes what actually
    happened (not whatever an LLM would hallucinate), and tag the exit so
    handle_exit() can surface it as an abort rather than a completion.
    """

    def __init__(self, name, blackboard, agent_registry, tool_registry):
        super().__init__(name, blackboard, agent_registry, tool_registry)

    def _build_abort_report(self) -> dict:
        """Gather abort context from the blackboard into a structured report."""
        # last_agent coming into the graceful_exit loop is the exit_state
        # handle_graceful_exit seeded (e.g. "graceful_exit", "max_limit",
        # "error_exit"). We use it as the reason label.
        exit_state = str(self.blackboard.get_state_value("last_agent", "") or "").strip() or "unknown"
        # The TRUE abort reason + the aborted run's cycle count are stashed by
        # handle_graceful_exit before the exit loop re-enters _run_loop (which
        # overwrites manager_loop_count and re-labels last_agent).
        true_reason = str(self.blackboard.get_state_value("manager_abort_reason", "") or "").strip()
        if true_reason:
            exit_state = true_reason
        manager_name = str(self.blackboard.get_state_value("manager_name", "") or "").strip() or "manager"
        loop_count = self.blackboard.get_state_value("manager_aborted_cycles", None)
        if loop_count is None:
            loop_count = self.blackboard.get_state_value("manager_loop_count", None)
        max_cycles = self.blackboard.get_state_value("manager_max_cycles", None)
        error_message = str(self.blackboard.get_state_value("error_message", "") or "").strip()
        task_text = str(self.blackboard.get_state_value("task", "") or "").strip()

        reason_map = {
            "max_limit": f"reached the maximum action budget ({max_cycles} cycles) without finishing",
            "graceful_exit": "triggered graceful-exit routing",
            "error_exit": f"hit an error ({error_message or 'unspecified'})",
            "default_error_exit": f"hit an error ({error_message or 'unspecified'})",
        }
        reason_text = reason_map.get(exit_state, f"exited via '{exit_state}'")

        summary_parts = [f"Task aborted: {manager_name} {reason_text}."]
        if task_text:
            summary_parts.append(f"Task was: {task_text}")
        if loop_count is not None:
            summary_parts.append(f"Cycles run: {loop_count}.")
        if error_message and "error" not in reason_text:
            summary_parts.append(f"Error: {error_message}")
        summary_parts.append("No final answer was produced.")
        answer = " ".join(summary_parts)

        return {
            "final_answer_answer": answer,
            "aborted": True,
            "exit_state": exit_state,
            "manager_name": manager_name,
            "manager_loop_count": loop_count,
            "manager_max_cycles": max_cycles,
            "error_message": error_message or None,
            "task": task_text or None,
        }

    def action_handler(self, message):
        report = self._build_abort_report()
        logger.warning(
            "[%s] Manager aborted: exit_state=%r cycles=%s/%s error=%r",
            self.name,
            report["exit_state"],
            report["manager_loop_count"],
            report["manager_max_cycles"],
            report["error_message"],
        )

        # Deterministic final_answer so handle_exit has structured content to
        # return — instead of whatever (possibly empty) state the last loop
        # cycle left behind.
        self.blackboard.update_state_value("final_answer", report)
        # Flag for handle_exit to differentiate aborted exits from successes.
        self.blackboard.update_state_value("manager_exit_kind", "aborted")

        # Preserve the long-form contextual message for anyone (downstream
        # agents, tests, traces) reading the blackboard history.
        self.blackboard.append_state_value(
            "final_answer_content",
            report["final_answer_answer"],
        )
        self.blackboard.add_msg(
            Message(sender="graceful_exit_node", content=report["final_answer_answer"])
        )

        # Only return-to-caller for true nested contexts.
        # Root manager scope is stored as (manager_name, manager_name, root_scope_id) and must not
        # be popped here, otherwise next_agent can become manager_name (not an agent), causing routing errors.
        current_context = self.blackboard.get_current_call_context()
        manager_name = self.blackboard.get_state_value("manager_name")
        is_nested_subtask = False
        if isinstance(current_context, tuple) and len(current_context) == 3:
            calling_agent = current_context[0]
            called_agent = current_context[1]
            if (
                isinstance(calling_agent, str)
                and isinstance(called_agent, str)
                and isinstance(manager_name, str)
                and calling_agent.strip()
                and called_agent.strip()
            ):
                is_nested_subtask = (
                    calling_agent.strip() != manager_name.strip()
                    or called_agent.strip() != manager_name.strip()
                )

        if is_nested_subtask:
            routed = self._pop_and_route_to_calling_agent()
            if routed:
                logger.info("[%s] Graceful exit - returning control to calling agent.", self.name)
                self.blackboard.update_state_value("last_agent", self.name)
                return

        # No parent context: terminate manager loop. handle_exit() will read
        # `final_answer` + `manager_exit_kind` and emit a properly-tagged
        # aborted ToolResult.
        self.blackboard.update_global_state_value("exit", True)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)
