"""emi_code chat-task router — like KgDevChatTaskRouterNode but targets
the claude_code_invoke tool instead of kg_dev_manager.

EmiCode has only one downstream tool (the coding-agent invoker), so the
chat gate's handoff goes straight into a synthesized tool_arguments
payload. Saves the switchboard LLM call.

All routing/guard/ack logic is inherited from KgDevChatTaskRouterNode;
this subclass exists only to swap TARGET_TOOL.
"""
from __future__ import annotations

from app.assistant.control_nodes.kg_dev_chat_task_router_node import (
    KgDevChatTaskRouterNode,
)


class EmiCodeChatTaskRouterNode(KgDevChatTaskRouterNode):
    TARGET_TOOL = "claude_code_invoke"
