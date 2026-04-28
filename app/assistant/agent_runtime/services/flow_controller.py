from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import set_pending_tool

logger = get_logger(__name__)


class FlowController:
    """
    Shared flow routing for scalar action payloads.
    """

    def route(self, agent, result_dict: dict) -> None:
        action = result_dict.get("action")
        if not action:
            return

        if action == "error":
            logger.error("[%s] Invalid action='error' reached flow controller.", agent.name)
            raise ValueError(f"[{agent.name}] action='error' is not allowed.")

        if action == "return_control":
            nested_call_scope = self._is_nested_call_scope(agent)
            if result_dict.get("result"):
                agent.blackboard.update_state_value("result", result_dict.get("result"))
            else:
                agent.blackboard.update_state_value("result", result_dict)

            if nested_call_scope:
                # Called-agent return path:
                # keep routing fields untouched so ToolResultHandler decides where to go
                # after it pops the child scope.
                logger.info(
                    "[%s] return_control in nested call scope; deferring next/last routing to ToolResultHandler.",
                    agent.name,
                )
                return

            exit_signal = f"{agent.name}_return_control"
            agent.blackboard.update_state_value("last_agent", exit_signal)
            agent.blackboard.update_state_value("next_agent", None)
            return

        if action == "done":
            if result_dict.get("result"):
                agent.blackboard.update_state_value("result", result_dict.get("result"))
            if self._is_nested_call_scope(agent):
                logger.info(
                    "[%s] done in nested call scope; deferring next/last routing to ToolResultHandler.",
                    agent.name,
                )
                return
            agent.blackboard.update_state_value("last_agent", agent.name)
            agent.blackboard.update_state_value("next_agent", None)
            return

        normalized_arguments = self._initial_pending_arguments(agent=agent, action=action, action_input=result_dict.get("action_input"))
        set_pending_tool(
            agent.blackboard,
            name=action,
            calling_agent=agent.name,
            action_input=result_dict.get("action_input"),
            arguments=normalized_arguments,
            kind=None,
        )
        logger.info(
            "[%s] Pending call recorded; next_agent not set here. Delegator/state_map will route.",
            agent.name,
        )

    @staticmethod
    def _is_nested_call_scope(agent) -> bool:
        try:
            ctx = agent.blackboard.get_current_call_context()
        except Exception:
            logger.debug("[%s] Could not read call context for nested scope check", agent.name, exc_info=True)
            return False
        if not isinstance(ctx, tuple) or len(ctx) < 2:
            return False
        caller = ctx[0]
        callee = ctx[1]
        return isinstance(caller, str) and isinstance(callee, str) and caller != callee

    def _initial_pending_arguments(self, *, agent, action: str, action_input):
        """
        Initialize pending_tool.arguments from planner action_input for tool actions.

        Contract:
        - Do not fail tool calls at planner stage due to action_input shape.
        - ToolArgumentsAgent is the canonical normalizer/validator for tool arguments.
        - Keep any raw planner action_input on pending_tool.action_input and only
          pass dict values through as initial pending arguments.
        """
        is_tool = False
        try:
            is_tool = bool(agent.tool_registry.get_tool(action))
        except Exception as e:
            logger.error("[%s] Failed tool lookup for action '%s': %s", agent.name, action, e)
            logger.debug("[%s] tool lookup exception details", agent.name, exc_info=True)
            raise

        if not is_tool:
            return None

        if action_input is None:
            return {}

        if isinstance(action_input, dict):
            return action_input

        logger.debug(
            "[%s] Non-dict action_input for tool '%s'; ToolArguments will normalize from the raw string.",
            agent.name,
            action,
        )
        return {}
