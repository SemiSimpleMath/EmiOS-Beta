"""kg_dev chat-task router — like ChatTaskRouterNode but skips the switchboard
agent. The dev console only has one tool (kg_dev_manager), so the gate's
handoff goes straight into a synthesized tool_arguments payload routed at
the tool_caller. Saves one LLM call per turn.

Inherits helpers (_send_ack_to_user, _resolve_reply_to, _cfg) from
ChatTaskRouterNode but reimplements action_handler so we don't need the
base's switchboard_agent key in flow_config.
"""
from __future__ import annotations

from app.assistant.control_nodes.chat_task_router_node import ChatTaskRouterNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class KgDevChatTaskRouterNode(ChatTaskRouterNode):
    """Chat-task router for the kg_dev console.

    Replaces switchboard handoff with a direct-to-tool synthesis targeting
    kg_dev_manager. Persists an in-flight guard so the gate doesn't refire
    while the planner is still running.
    """

    TARGET_TOOL = "kg_dev_manager"

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        source_agent = self._required_str(cfg, "source_agent")
        final_node = self._required_str(cfg, "final_node")
        handoff_flag_key = self._optional_str(cfg, "exit_flag_key", "handoff_tf")
        no_op_flag_key = self._optional_str(cfg, "no_op_flag_key", "no_op_tf")
        chat_response_key = self._optional_str(cfg, "chat_response_key", "chat_response")
        switchboard_task_key = self._optional_str(cfg, "switchboard_task_key", "switchboard_task")
        switchboard_information_key = self._optional_str(cfg, "switchboard_information_key", "switchboard_information")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != source_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={source_agent} before chat-task route, got: {last_agent!r}"
            )

        no_op_tf = bool(self.blackboard.get_state_value(no_op_flag_key, False))
        should_handoff = bool(self.blackboard.get_state_value(handoff_flag_key, False))

        if no_op_tf and should_handoff:
            raise ValueError(
                f"[{self.name}] at most one of {no_op_flag_key}, {handoff_flag_key} may be true."
            )

        # No-op
        if no_op_tf:
            self.blackboard.update_state_value(
                "result",
                {
                    "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                    "final_answer_answer": "",
                    "final_answer_no_op": True,
                    "final_answer_what_was_done": "No-op at chat gate.",
                    "final_answer_interesting_info": "",
                    "final_answer_sources": [],
                },
            )
            self.blackboard.update_state_value("next_agent", final_node)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Direct chat reply
        if not should_handoff:
            chat_response = self.blackboard.get_state_value(chat_response_key, "")
            if not isinstance(chat_response, str) or not chat_response.strip():
                raise ValueError(
                    f"[{self.name}] {chat_response_key!r} must be non-empty when {handoff_flag_key}=false."
                )
            self.blackboard.update_state_value(
                "result",
                {
                    "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                    "final_answer_answer": chat_response.strip(),
                    "final_answer_what_was_done": "Answered directly in chat gate.",
                    "final_answer_interesting_info": "",
                    "final_answer_sources": [],
                },
            )
            self.blackboard.update_state_value("next_agent", final_node)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Handoff: skip switchboard, synthesize tool_arguments directly.
        switchboard_task = self.blackboard.get_state_value(switchboard_task_key, "")
        if not isinstance(switchboard_task, str) or not switchboard_task.strip():
            raise ValueError(
                f"[{self.name}] {switchboard_task_key!r} must be non-empty when {handoff_flag_key}=true."
            )
        switchboard_information = self.blackboard.get_state_value(switchboard_information_key, "")
        if switchboard_information is None:
            switchboard_information = ""
        if not isinstance(switchboard_information, str):
            raise ValueError(
                f"[{self.name}] {switchboard_information_key!r} must be a string when provided."
            )

        task_text = switchboard_task.strip()
        info_text = switchboard_information.strip()

        # Ack to user before tool work starts.
        chat_response = str(self.blackboard.get_state_value(chat_response_key, "") or "").strip() or "On it."
        self._send_ack_to_user(chat_response)

        # Synthesize the tool call. Mirror ChatSwitchboardArgumentsNode shape so
        # the downstream tool_caller is unchanged.
        arguments = {"task": task_text, "information": info_text}
        self.blackboard.update_state_value("task", task_text)
        self.blackboard.update_state_value("information", info_text)
        self.blackboard.update_state_value("action", self.TARGET_TOOL)
        self.blackboard.update_state_value("action_input", arguments)
        self.blackboard.update_state_value(
            "tool_arguments",
            {"target_name": self.TARGET_TOOL, "arguments": arguments},
        )
        self.blackboard.update_state_value("result", None)

        self._persist_guard(guard_label=f"Task: {task_text}\nArgs: {info_text or '(none)'}")

        self.blackboard.update_state_value("calling_agent", None)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)
