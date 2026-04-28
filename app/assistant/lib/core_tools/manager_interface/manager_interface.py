# File: assistant/lib/core_tools/manager_interface.py

import uuid

from app.assistant.utils.pydantic_classes import ToolMessage, Message, ToolResult
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


"""
When a manager calls another manager it does it as a tool call in a sync manner.

When a manager is called via async manner with event hub, it will return the result via event hub.
"""

class ManagerInterface:
    """
    Generic interface for executing manager requests, handling both direct calls and event hub requests.

    Tool visibility narrowing is intentionally NOT performed here. It belongs inside
    the manager's own tool_scope_service.initialize_scope(), which has full access to
    the manager config (hidden_tools, always_show, use_narrower) and runs after the
    manager is instantiated with the correct allowed tool set.
    """

    def __init__(self, manager_name: str):
        self.manager_name = manager_name

    def _should_trace_emi_ingress(self) -> bool:
        return str(self.manager_name or "").strip() == "emi_team_manager"

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data or {}
        args = tool_data.get('arguments', {}) if isinstance(tool_data.get('arguments'), dict) else {}
        task = args.get('task')
        information = args.get('information')
        data = tool_data.get('data') if isinstance(tool_data.get('data'), dict) else {}

        task_file = args.get('task_file')
        if isinstance(task_file, str) and task_file.strip():
            data = dict(data)
            data["task_file"] = task_file.strip()
        request_id = tool_message.request_id
        inherited_scope = getattr(tool_message, "scope_context", None)
        if self._should_trace_emi_ingress():
            logger.info(
                "[emi_team ingress] execute start request_id=%s has_task_file=%s has_inherited_scope=%s data_keys=%s",
                request_id,
                bool(isinstance(task_file, str) and task_file.strip()),
                bool(inherited_scope),
                sorted(list(data.keys())) if isinstance(data, dict) else [],
            )

        try:
            invocation_name = f"{self.manager_name}_{uuid.uuid4().hex[:8]}"
            self.manager = DI.multi_agent_manager_factory.create_manager(
                self.manager_name, name=invocation_name
            )
        except Exception as e:
            logger.error("Failed to create manager instance for %s: %s", self.manager_name, e)
            logger.debug("manager creation exception details", exc_info=True)
            raise RuntimeError(f"Failed to create a manager for {self.manager_name}: {e}")

        manager_content = None
        if task:
            manager_content = task
        elif tool_message.content:
            manager_content = tool_message.content
        elif tool_message.tool_data.get('arguments', {}).get('question'):
            manager_content = tool_message.tool_data.get('arguments', {}).get('question')
        elif information:
            manager_content = information
        else:
            manager_content = f"Process request for {self.manager_name}"

        manager_message = Message(
            event_topic="task_request",
            sender=self.manager_name,
            receiver=None,
            content=manager_content,
            task=task,
            information=information,
            request_id=None,
            data=data,
            scope_context=inherited_scope,
        )
        logger.info(f"{self.manager_name.capitalize()}: Processing content '{manager_content[:50]}...' with ID {request_id}")

        try:
            logger.debug(
                "[%s] Dispatching manager message request_id=%s data_keys=%s",
                self.manager_name,
                request_id,
                sorted(list(data.keys())) if isinstance(data, dict) else [],
            )

            result = DI.manager_invoker.invoke(self.manager, manager_message)

            logger.info(f"{self.manager_name.capitalize()}: Received result.")

            # Always return synchronously. The caller (tool_caller) expects
            # a ToolResult back. The old async event_hub path (request_id check)
            # was firing on every call because request_id leaks from the parent
            # context, causing the synchronous caller to receive None and crash.
            return result

        except Exception as e:
            logger.error(f"{self.manager_name.capitalize()} execution failed: %s", e)
            logger.debug("%s manager execution exception details", self.manager_name, exc_info=True)
            error_result = make_tool_error(
                error_code="manager_interface_execute_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"manager_name": self.manager_name},
            )
            return error_result
