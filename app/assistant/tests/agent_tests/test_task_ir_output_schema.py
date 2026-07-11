import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.task_ir_runtime.task_ir_runner import TaskIRRunner
from app.assistant.utils.pydantic_classes import ToolResult


def test_output_schema_bool_accepts_boolean_json_literal():
    runner = TaskIRRunner()
    step = {
        "id": "s_bool",
        "kind": "action",
        "produces_data_ids": ["fact_1"],
        "output_schema": {"type": "bool"},
    }
    result = ToolResult(
        result_type="final_answer",
        content="ok",
        data={"final_answer_data_list": [{"key": "fact_1", "value": "true"}]},
    )

    update = runner._normalize_manager_result_to_task_state_update(step=step, result=result)
    assert isinstance(update, dict)
    assert update.get("facts", {}).get("fact_1") is True


def test_output_schema_bool_rejects_non_boolean_value():
    runner = TaskIRRunner()
    step = {
        "id": "s_bool_bad",
        "kind": "action",
        "produces_data_ids": ["fact_1"],
        "output_schema": {"type": "bool"},
    }
    result = ToolResult(
        result_type="final_answer",
        content="ok",
        data={"final_answer_data_list": [{"key": "fact_1", "value": "1"}]},
    )

    with pytest.raises(ValueError, match="must be bool"):
        _ = runner._normalize_manager_result_to_task_state_update(step=step, result=result)


def test_gate_type_alias_maps_to_schema_and_enforces_type():
    runner = TaskIRRunner()
    step = {
        "id": "s_gate_int",
        "kind": "action",
        "produces_data_ids": ["fact_7"],
        "gate_type": "int",
    }
    ok = ToolResult(
        result_type="final_answer",
        content="ok",
        data={"final_answer_data_list": [{"key": "fact_7", "value": "12"}]},
    )
    bad = ToolResult(
        result_type="final_answer",
        content="ok",
        data={"final_answer_data_list": [{"key": "fact_7", "value": "12.5"}]},
    )

    update = runner._normalize_manager_result_to_task_state_update(step=step, result=ok)
    assert update.get("facts", {}).get("fact_7") == 12

    with pytest.raises(ValueError, match="must be int"):
        _ = runner._normalize_manager_result_to_task_state_update(step=step, result=bad)


def test_preloaded_artifact_repo_file_reference_loads_at_runtime():
    runner = TaskIRRunner()
    context = runner._normalize_context({})
    task = {
        "task_id": "task_preloaded_ref_test",
        "entry_step_id": "s1",
        "steps": [{"id": "s1", "kind": "end"}],
        "preloaded_task_state": {
            "facts": {},
            "flags": {},
            "artifacts": {
                "artifact_1": {
                    "__resource_ref__": {
                        "kind": "repo_file",
                        "path": "tasks/TASK_SPEC_TEMPLATE.md",
                    }
                }
            },
        },
    }

    runner._apply_preloaded_task_state(task=task, context=context)
    artifact_1 = context["task_state"]["artifacts"].get("artifact_1")
    assert isinstance(artifact_1, str)
    assert "Resources and includes" in artifact_1
