"""The latest playwright snapshot card must be visible to TOOLS.

web_fill_ref / web_click_ref_snapshot resolve a ref to its element label by
reading ``playwright_latest_snapshot`` from DI.global_blackboard (tools run
outside the manager's blackboard). Before 2026-07-08 only the manager-local
blackboard was written, so the tools' lookup always found None and the label
hint never reached the MCP call (runtime audit B4). This pins the dual write.
"""
from __future__ import annotations

from app.assistant.control_nodes.tool_result_handler import ToolResultHandler
from app.assistant.control_nodes import tool_result_handler as trh_module
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message, ToolResult


def test_snapshot_card_published_to_global_blackboard(monkeypatch):
    handler = ToolResultHandler.__new__(ToolResultHandler)
    handler.name = "tool_result_handler"
    handler.blackboard = Blackboard()

    global_bb = Blackboard()
    monkeypatch.setattr(trh_module.DI, "global_blackboard", global_bb, raising=False)

    tool_result = ToolResult(
        result_type="web_snapshot",
        content="snapshot taken",
        data={
            "snapshot_id": "snap_test_1",
            "url": "https://example.test/page",
            "actionable_elements": [
                {"ref": "e1", "role": "button", "text": "Submit order"},
            ],
        },
    )
    handler._update_latest_playwright_snapshot_state(
        message=Message(data_type="tool_result", content="snapshot taken"),
        tool_result=tool_result,
    )

    local_card = handler.blackboard.get_state_value("playwright_latest_snapshot")
    assert isinstance(local_card, dict) and local_card.get("snapshot_id") == "snap_test_1"

    # The tool-visible copy — what web_fill_ref / web_click_ref_snapshot read.
    global_card = global_bb.get_state_value("playwright_latest_snapshot")
    assert isinstance(global_card, dict) and global_card.get("snapshot_id") == "snap_test_1"
    labels = [e.get("text") for e in global_card.get("actionable_elements") or []]
    assert "Submit order" in labels
