from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message, ToolResult
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


class call_agent(BaseTool):
    def __init__(self):
        super().__init__('call_agent')

    def execute(self, tool_message):
        tool_data = tool_message.tool_data or {}
        args = tool_data.get('arguments', {}) if isinstance(tool_data.get('arguments'), dict) else {}

        agent_name = str(args.get('agent_name', '')).strip()
        if not agent_name:
            return make_tool_error(
                error_code="missing_agent_name",
                message="agent_name is required.",
                abort_policy="abort_tool",
                retryable=False,
            )

        task = str(args.get('task', '') or '').strip()
        inputs = args.get('inputs', {})
        if not isinstance(inputs, dict):
            inputs = {}

        # Verify agent exists
        agent_config = DI.agent_registry.get_agent_config(agent_name)
        if not agent_config:
            return make_tool_error(
                error_code="agent_not_found",
                message=f"Agent '{agent_name}' not found in registry.",
                abort_policy="abort_tool",
                retryable=False,
            )

        try:
            blackboard = Blackboard()
            agent = DI.agent_factory.create_agent(agent_name, blackboard=blackboard)

            message = Message(
                event_topic="task_request",
                sender="call_agent",
                receiver=agent_name,
                content=task or f"Execute {agent_name}.",
                task=task,
                agent_input=inputs,
            )

            result = agent.action_handler(message)

            result_data = result.data if hasattr(result, 'data') else {}
            return ToolResult(
                result_type="agent_result",
                content=result_data.get("summary", result_data.get("final_answer_answer", f"{agent_name} completed.")),
                data=result_data,
            )

        except Exception as e:
            logger.error("call_agent failed for '%s': %s", agent_name, e)
            logger.debug("call_agent exception details", exc_info=True)
            return make_tool_error(
                error_code="call_agent_failed",
                message=f"{agent_name}: {e}",
                abort_policy="abort_tool",
                retryable=False,
            )


def get_tool_class():
    return call_agent
