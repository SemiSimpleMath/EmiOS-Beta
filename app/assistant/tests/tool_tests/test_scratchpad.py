"""The scratchpad holds text written during a task: its result reaches the agents that follow.

2026-11-06: personal_admin wrote a drafted email in its return_control, which the final answer agent's history leaves
out, and the draft was lost. Written with this tool, it is a tool result, which that history carries.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.assistant.agent_runtime.services.history_formatter import HistoryFormatter
from app.assistant.lib.tools.scratchpad.scratchpad import ScratchpadTool
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message, ToolMessage

DRAFT = "To: A Friend\nSubject: Hello\n\nHi there,\nSee you Friday."


def _run(arguments: dict):
    return ScratchpadTool().execute(ToolMessage(tool_name="scratchpad", tool_data={"arguments": arguments}))


def test_the_text_comes_back_exactly_as_written():
    result = _run({"text": DRAFT})
    assert result.result_type == "success"
    assert result.content == DRAFT
    assert result.data == {"text": DRAFT}


def test_empty_text_is_an_error():
    assert _run({"text": "  "}).result_type == "error"


def test_history_carries_the_result_for_the_final_answer():
    message = Message(data_type="tool_result", sub_data_type=["scratchpad"], sender="tool", receiver="Blackboard",
                      content=_run({"text": DRAFT}).content)
    assert DRAFT in HistoryFormatter().format_recent_history([message])


def test_personal_admin_can_reach_it():
    root = Path(get_repo_root()) / "app" / "assistant"
    planner = yaml.safe_load((root / "agents/personal_admin/planner/config.yaml").read_text(encoding="utf-8"))
    manager = yaml.safe_load((root / "multi_agents/personal_admin_manager/config.yaml").read_text(encoding="utf-8"))
    assert "scratchpad" in planner["allowed_tools"]
    assert "scratchpad" in manager["tools"]["allowed_tools"]
    assert "scratchpad" in manager["scope_contract"]["tools"]["allowed_tools"]
