"""Shared normalization for switchboard-style argument nodes.

The chat-side, master-room, and dayflow argument nodes each translate the
switchboard agent's blackboard output (delegate_to / task / task_information)
into a canonical tool-call payload (action / action_input / tool_arguments).
That translation is pure and identical across the three; the divergence is
in what happens AROUND it (master-room writes a dayflow marker, dayflow
writes its own provenance, basic chat writes neither).

This util holds only the truly-shared normalization. The three nodes
(ChatSwitchboardArgumentsNode, MasterRoomSwitchboardArgumentsNode,
DayflowSwitchboardArgumentsNode) each extend ControlNode directly and call
``normalize_switchboard_args`` for the shared piece.
"""
from __future__ import annotations

from typing import Any, Dict


def normalize_switchboard_args(blackboard, *, name: str) -> Dict[str, Any]:
    """Read switchboard agent output, validate, build the canonical tool-call
    payload, and write action / action_input / tool_arguments back to the
    blackboard. Returns the arguments dict for callers that need it.

    Raises ValueError on missing/wrong-type inputs.

    Args:
        blackboard: caller's blackboard (must support get_state_value /
            update_state_value).
        name: caller's node name, used in error messages.
    """
    last_agent = blackboard.get_state_value("last_agent", None)
    if not isinstance(last_agent, str) or not last_agent.strip():
        raise ValueError(
            f"[{name}] last_agent must be set before switchboard argument mapping."
        )

    delegate_to = blackboard.get_state_value("delegate_to", "")
    task = blackboard.get_state_value("task", "")
    task_information = blackboard.get_state_value("task_information", "")

    if not isinstance(delegate_to, str) or not delegate_to.strip():
        raise ValueError(f"[{name}] delegate_to must be a non-empty string.")
    if task_information is None:
        task_information = ""
    if not isinstance(task_information, (str, dict)):
        raise ValueError(
            f"[{name}] task_information must be a string or object when provided."
        )

    selected_tool = delegate_to.strip()
    if not isinstance(task, str) or not task.strip():
        raise ValueError(f"[{name}] task must be a non-empty string.")

    information_value: Any
    if isinstance(task_information, dict):
        information_value = task_information
    else:
        information_value = str(task_information).strip()

    arguments: Dict[str, Any] = {
        "task": task.strip(),
        "information": information_value,
    }
    if selected_tool == "run_task":
        arguments["task_query"] = task.strip()

    blackboard.update_state_value("action", selected_tool)
    blackboard.update_state_value("action_input", arguments)
    blackboard.update_state_value(
        "tool_arguments",
        {"target_name": selected_tool, "arguments": arguments},
    )

    return arguments
