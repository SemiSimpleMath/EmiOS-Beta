"""The visible tool list keeps its order when a tool is used.

2026-11-15: record_tool_result moved each used tool to the front of `visible_tools`, which is the order the
planner's tool list renders in, so the prompt changed between turns (devices_manager jumped first after one
call) and no turn reused the previous one's prompt cache.
"""
from app.assistant.manager_runtime.services.tool_scope_state_manager import ToolScopeStateManager
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _manager(visible):
    bb = FakeBlackboard({})
    mgr = ToolScopeStateManager(bb)
    mgr.initialize_runtime_scope(visible_tools=visible, max_visible=-1)
    return bb, mgr


def test_using_a_visible_tool_keeps_the_order():
    bb, mgr = _manager(["find_tool", "pod_search", "devices_manager"])
    mgr.record_tool_result(selected_tool="devices_manager")
    assert bb.get_state_value("visible_tools") == ["find_tool", "pod_search", "devices_manager"]
    assert bb.get_state_value("recently_used_tools") == ["devices_manager"]


def test_a_tool_not_yet_visible_is_added():
    bb, mgr = _manager(["find_tool", "pod_search"])
    mgr.record_tool_result(selected_tool="devices_manager")
    assert bb.get_state_value("visible_tools") == ["devices_manager", "find_tool", "pod_search"]
