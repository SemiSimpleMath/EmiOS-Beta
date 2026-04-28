import json
from pathlib import Path

import yaml

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.path_utils import get_repo_root

_REPO = get_repo_root()

TASK_FILES = [
    str(_REPO / "tasks/timesheet/task_spec.md"),
    str(_REPO / "tasks/morning_routine/task_spec.md"),
    str(_REPO / "tasks/activity_log/task_spec.md"),
]


def _load_task_spec(path: str) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Task spec '{path}' missing YAML frontmatter start.")
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError(f"Task spec '{path}' missing YAML frontmatter end.")
    frontmatter_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()
    frontmatter = yaml.safe_load(frontmatter_text)
    if not isinstance(frontmatter, dict):
        raise TypeError(f"Task spec '{path}' frontmatter must be a dict.")
    return frontmatter, body


class _CatalogAwareCompileStub:
    def structured_output(self, messages, use_json=False, **params):
        _ = use_json
        _ = params
        user_prompt = ""
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                user_prompt = str(msg.get("content") or "")
                break

        # Ensure compiler gets a manager-aware context.
        for required in ("playwright_manager", "personal_admin_manager", "web_manager"):
            if required not in user_prompt:
                raise AssertionError(f"Manager catalog missing '{required}' in compile prompt.")

        task_id = ""
        if "timesheet_narratives_v1" in user_prompt:
            task_id = "timesheet_narratives_v1"
            steps = [
                {
                    "id": "step_1",
                    "kind": "action",
                    "title": "Read and transform timesheet",
                    "executor": "emi_team_manager",
                    "instruction": "Read timesheet input, format narratives, and write output file.",
                    "next_step": "step_2",
                },
                {"id": "step_2", "kind": "end", "title": "Done"},
            ]
        elif "morning_routine_v1" in user_prompt:
            task_id = "morning_routine_v1"
            steps = [
                {
                    "id": "step_1",
                    "kind": "action",
                    "title": "Collect email and calendar",
                    "executor": "personal_admin_manager",
                    "instruction": "Fetch email window and today's calendar events.",
                    "next_step": "step_2",
                },
                {
                    "id": "step_2",
                    "kind": "action",
                    "title": "Collect web headlines",
                    "executor": "web_manager",
                    "instruction": "Get CNN and Daily Kos highlights.",
                    "next_step": "step_3",
                },
                {
                    "id": "step_3",
                    "kind": "action",
                    "title": "Render HTML report",
                    "executor": "emi_team_manager",
                    "instruction": "Compose and write final morning HTML report.",
                    "next_step": "step_4",
                },
                {"id": "step_4", "kind": "end", "title": "Done"},
            ]
        elif "activity_log_screen_caps_2026_02_08" in user_prompt:
            task_id = "activity_log_screen_caps_2026_02_08"
            steps = [
                {
                    "id": "step_1",
                    "kind": "action",
                    "title": "Capture and summarize screens",
                    "executor": "emi_team_manager",
                    "instruction": "Capture monitors and write final call to output markdown.",
                    "next_step": "step_2",
                },
                {"id": "step_2", "kind": "end", "title": "Done"},
            ]
        else:
            raise AssertionError("Task id not found in compile prompt.")

        compiled_task = {
            "schema_version": "task_ir_v1",
            "task_id": task_id,
            "created_at_utc": "2026-02-22T00:00:00+00:00",
            "compiler_name": "task_compile_manager",
            "compiler_version": "v1",
            "source_task": "",
            "source_information": "",
            "source_hash": "abc123abc123abcd",
            "entry_step_id": "step_1",
            "steps": steps,
        }
        return {
            "what_i_am_thinking": "Using manager-aware compile plan.",
            "summary": "Compiled task using manager boundaries.",
            "checklist": ["parsed task", "mapped executors", "generated IR"],
            "progress": ["done"],
            "plan": "compile",
            "action": "return_control",
            "action_input": "{}",
            "phase_i_atoms": {
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "watch_event",
                        "title": "Primary task event",
                        "description": "Primary trigger extracted from task prose.",
                        "canonical_event_name": "signal_router.watch.primary",
                    }
                ],
                "actions": [
                    {
                        "id": "action_1",
                        "executor": "emi_team_manager",
                        "title": "Primary action block",
                        "instruction": "Execute the primary action block for this task.",
                    }
                ],
            },
            "compiled_task": compiled_task,
            "logic_tree": {
                "entry_step_id": "step_1",
                "edges": [
                    {"from_step_id": "step_1", "to_step_id": "step_2", "reason": "next_step"},
                ],
                "bindings": [
                    {"step_id": "step_1", "atomic_type": "action", "atomic_id": "action_1"},
                    {"step_id": "step_1", "atomic_type": "event", "atomic_id": "event_1"},
                ],
            },
            "final_answer_data_list": [
                {
                    "data_type": "compiled_task",
                    "key": "compiled_task",
                    "value": json.dumps(compiled_task),
                }
            ],
            "final_answer_answer": f"Compiled {task_id}.",
        }


def test_compile_existing_task_specs_manager_aware(monkeypatch):
    monkeypatch.setattr(
        "app.assistant.agent_runtime.services.llm_client.LLMFactory.get_llm_interface",
        lambda **kwargs: _CatalogAwareCompileStub(),
    )
    DI.manager_registry.preload_all()

    expected_executors = {
        "timesheet_narratives_v1": {"emi_team_manager"},
        "morning_routine_v1": {"personal_admin_manager", "web_manager"},
        "activity_log_screen_caps_2026_02_08": {"emi_team_manager"},
    }

    for task_file in TASK_FILES:
        frontmatter, body = _load_task_spec(task_file)
        task_id = str(frontmatter.get("task_id") or "").strip()
        if not task_id:
            raise ValueError(f"Task file '{task_file}' missing task_id.")

        manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")
        request = Message(
            data_type="agent_activation",
            sender="User",
            receiver="Delegator",
            content="",
            task=body,
            information=json.dumps(
                {
                    "task_file": task_file,
                    "task_id": task_id,
                    "task_description": frontmatter.get("description", ""),
                }
            ),
        )
        result = manager.request_handler(request)
        assert result.result_type == "final_answer"
        assert isinstance(result.data, dict)

        data_list = result.data.get("final_answer_data_list", [])
        if not isinstance(data_list, list):
            raise TypeError("final_answer_data_list must be a list.")
        compiled_item = next((x for x in data_list if isinstance(x, dict) and x.get("key") == "compiled_task"), None)
        assert isinstance(compiled_item, dict)
        compiled_json = compiled_item.get("value")
        assert isinstance(compiled_json, str)
        compiled = json.loads(compiled_json)

        steps = compiled.get("steps", [])
        assert isinstance(steps, list)
        action_executors = {
            str(step.get("executor")).strip()
            for step in steps
            if isinstance(step, dict) and step.get("kind") == "action" and isinstance(step.get("executor"), str)
        }
        assert expected_executors[task_id].issubset(action_executors)

        print(f"COMPILED_PREVIEW [{task_id}]")
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            print(
                f"  {idx}) {step.get('title', '')} [{step.get('kind', '')}] "
                f"executor={step.get('executor', '')} next={step.get('next_step', '')}"
            )
