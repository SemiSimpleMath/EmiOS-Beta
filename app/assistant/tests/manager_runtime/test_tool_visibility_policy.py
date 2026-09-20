from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.tests.dayflow.conftest import FakeBlackboard


@pytest.mark.parametrize("pinned", [False, True])
def test_visibility_applies_task_policy_before_narrowing(pinned):
    bb = FakeBlackboard({"task_allowed_tools": ["allowed", "excepted"], "task_except_tools": ["excepted"], "visible_tools": ["allowed", "excepted", "forbidden"] if pinned else []})
    registry = SimpleNamespace(get_all_tools=lambda: {name: {} for name in ("allowed", "excepted", "forbidden")})
    service = ToolScopeService()
    service._run_narrower = Mock(side_effect=lambda **kwargs: kwargs["ranked"])
    service.initialize_scope(blackboard=bb, tool_registry=registry, manager_config={"tool_visibility": {"use_narrower": True}}, task="inspect", information="")
    assert bb.get_state_value("visible_tools") == ["allowed"]
    if not pinned:
        assert service._run_narrower.call_args.kwargs["ranked"] == ["allowed"]
