import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.task_ir_runtime.task_ir_runner import TaskIRRunner
from app.assistant.utils.pydantic_classes import ToolResult


class _FakeAPScheduler:
    def get_jobs(self):
        return []

    def get_job(self, job_id):  # noqa: ANN001
        return None

    def add_job(self, **kwargs):  # noqa: ANN001
        pass

    def remove_job(self, job_id):  # noqa: ANN001
        pass


class _FakeTimingEngine:
    scheduler = _FakeAPScheduler()


class _FakeSchedulerService:
    timing_engine = _FakeTimingEngine()


class _FakeBlackboard:
    def __init__(self, initial=None):
        self._state = dict(initial or {})

    def get_state_value(self, key):  # noqa: ANN001
        return self._state.get(key)

    def update_state_value(self, key, value):  # noqa: ANN001
        self._state[key] = value


class _FakeToolRegistry:
    def __init__(self, tools=None):
        self._tools = tools or {}

    def get_all_tools(self):
        return self._tools

    def get_tool_descriptions(self, names):  # noqa: ANN001
        return {n: f"desc:{n}" for n in names}


def _make_compiled_task(pinned_tools=None):
    step = {
        "id": "s1",
        "kind": "action",
        "title": "Capture and write",
        "executor": "emi_team_manager",
        "instruction": "Capture monitors and write activity log.",
        "next_step": "s2",
    }
    if pinned_tools is not None:
        step["pinned_tools"] = pinned_tools
    return {
        "schema_version": "task_ir_v1",
        "task_id": "test_pinned_tools",
        "entry_step_id": "s1",
        "steps": [
            step,
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }


# --- ToolScopeService unit tests ---


def test_initialize_scope_pinned_bypasses_ranker():
    """When visible_tools is pre-seeded, the ranker must never be called."""
    ranker_called = []

    service = ToolScopeService()
    original_resolve = service._resolve_ranker

    def _spy_resolve_ranker(vis_cfg):  # noqa: ANN001
        ranker_called.append(True)
        return original_resolve(vis_cfg)

    service._resolve_ranker = _spy_resolve_ranker

    bb = _FakeBlackboard({"visible_tools": ["capture_and_describe_monitors", "write_text_file"]})
    service.initialize_scope(
        blackboard=bb,
        tool_registry=_FakeToolRegistry({"capture_and_describe_monitors": {}, "write_text_file": {}, "other_tool": {}}),
        manager_config={"name": "emi_team_manager"},
        task="capture monitors and write file",
        information="",
    )

    assert not ranker_called, "Ranker must not be invoked when pinned_tools are pre-seeded"
    assert bb.get_state_value("visible_tools") == ["capture_and_describe_monitors", "write_text_file"]


def test_initialize_scope_pinned_ignores_hidden_tools():
    """pinned_tools must surface even tools that are in hidden_tools."""
    service = ToolScopeService()
    manager_config = {
        "name": "emi_team_manager",
        "tool_visibility": {
            "hidden_tools": ["capture_and_describe_monitors", "write_text_file"],
        },
    }

    bb = _FakeBlackboard({"visible_tools": ["capture_and_describe_monitors", "write_text_file"]})
    service.initialize_scope(
        blackboard=bb,
        tool_registry=_FakeToolRegistry({"capture_and_describe_monitors": {}, "write_text_file": {}}),
        manager_config=manager_config,
        task="capture monitors",
        information="",
    )

    assert bb.get_state_value("visible_tools") == ["capture_and_describe_monitors", "write_text_file"]


def test_initialize_scope_no_pinned_uses_ranker():
    """Without pre-seeded visible_tools, the ranker path runs normally."""
    ranker_called = []

    service = ToolScopeService()
    original_resolve = service._resolve_ranker

    def _spy_resolve_ranker(vis_cfg):  # noqa: ANN001
        ranker_called.append(True)
        return original_resolve(vis_cfg)

    service._resolve_ranker = _spy_resolve_ranker

    bb = _FakeBlackboard()
    service.initialize_scope(
        blackboard=bb,
        tool_registry=_FakeToolRegistry({"some_tool": {}}),
        manager_config={"name": "emi_team_manager"},
        task="do something",
        information="",
    )

    assert ranker_called, "Ranker must be invoked when no pinned_tools are present"


# --- End-to-end TaskIR tests ---


def test_pinned_tools_forwarded_as_visible_tools_in_data_payload(monkeypatch):
    """pinned_tools on an action step must arrive as visible_tools in data_payload."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(
        compiled_task=_make_compiled_task(
            pinned_tools=["capture_and_describe_monitors", "write_text_file"]
        ),
        initial_context={},
    )

    assert run_state["status"] == "completed"
    assert "data" in captured, "ManagerInterface.execute was never called"
    assert captured["data"]["visible_tools"] == [
        "capture_and_describe_monitors",
        "write_text_file",
    ]


def test_no_pinned_tools_means_no_visible_tools_in_data_payload(monkeypatch):
    """Steps without pinned_tools must NOT inject visible_tools into data_payload."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    runner.start_run(compiled_task=_make_compiled_task(), initial_context={})

    assert "visible_tools" not in captured.get("data", {}), (
        "visible_tools must not be injected when pinned_tools is absent"
    )


def test_pinned_tools_empty_list_is_ignored(monkeypatch):
    """An empty pinned_tools list must not inject visible_tools."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    runner.start_run(compiled_task=_make_compiled_task(pinned_tools=[]), initial_context={})

    assert "visible_tools" not in captured.get("data", {}), (
        "empty pinned_tools must not inject visible_tools"
    )


def test_pinned_tools_strips_blank_entries(monkeypatch):
    """Blank strings inside pinned_tools must be stripped before forwarding."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    runner.start_run(
        compiled_task=_make_compiled_task(
            pinned_tools=["capture_and_describe_monitors", "  ", "", "write_text_file"]
        ),
        initial_context={},
    )

    assert captured["data"]["visible_tools"] == [
        "capture_and_describe_monitors",
        "write_text_file",
    ]



class _FakeAPScheduler:
    def get_jobs(self):
        return []

    def get_job(self, job_id):  # noqa: ANN001
        return None

    def add_job(self, **kwargs):  # noqa: ANN001
        pass

    def remove_job(self, job_id):  # noqa: ANN001
        pass


class _FakeTimingEngine:
    scheduler = _FakeAPScheduler()


class _FakeSchedulerService:
    timing_engine = _FakeTimingEngine()


def _make_compiled_task(pinned_tools=None):
    step = {
        "id": "s1",
        "kind": "action",
        "title": "Capture and write",
        "executor": "emi_team_manager",
        "instruction": "Capture monitors and write activity log.",
        "next_step": "s2",
    }
    if pinned_tools is not None:
        step["pinned_tools"] = pinned_tools
    return {
        "schema_version": "task_ir_v1",
        "task_id": "test_pinned_tools",
        "entry_step_id": "s1",
        "steps": [
            step,
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }


def test_pinned_tools_forwarded_as_visible_tools_in_data_payload(monkeypatch):
    """pinned_tools on an action step must arrive as visible_tools in data_payload."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(
        compiled_task=_make_compiled_task(
            pinned_tools=["capture_and_describe_monitors", "write_text_file"]
        ),
        initial_context={},
    )

    assert run_state["status"] == "completed"
    assert "data" in captured, "ManagerInterface.execute was never called"
    assert captured["data"]["visible_tools"] == [
        "capture_and_describe_monitors",
        "write_text_file",
    ]


def test_no_pinned_tools_means_no_visible_tools_in_data_payload(monkeypatch):
    """Steps without pinned_tools must NOT inject visible_tools into data_payload."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    runner.start_run(compiled_task=_make_compiled_task(), initial_context={})

    assert "visible_tools" not in captured.get("data", {}), (
        "visible_tools must not be injected when pinned_tools is absent"
    )


def test_pinned_tools_empty_list_is_ignored(monkeypatch):
    """An empty pinned_tools list must not inject visible_tools."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    runner.start_run(compiled_task=_make_compiled_task(pinned_tools=[]), initial_context={})

    assert "visible_tools" not in captured.get("data", {}), (
        "empty pinned_tools must not inject visible_tools"
    )


def test_pinned_tools_strips_blank_entries(monkeypatch):
    """Blank strings inside pinned_tools must be stripped before forwarding."""
    monkeypatch.setattr(DI, "scheduler", _FakeSchedulerService(), raising=False)
    captured = {}

    def _fake_execute(self, tool_message):  # noqa: ANN001
        captured["data"] = dict(tool_message.tool_data.get("data", {}))
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    runner = TaskIRRunner(max_steps_per_tick=20)
    runner.start_run(
        compiled_task=_make_compiled_task(
            pinned_tools=["capture_and_describe_monitors", "  ", "", "write_text_file"]
        ),
        initial_context={},
    )

    assert captured["data"]["visible_tools"] == [
        "capture_and_describe_monitors",
        "write_text_file",
    ]
