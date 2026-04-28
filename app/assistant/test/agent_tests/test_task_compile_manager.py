import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
import json


class _StubLLMInterface:
    def structured_output(self, messages, use_json=False, **params):
        _ = messages
        _ = use_json
        _ = params
        compiled_task = {
            "schema_version": "task_ir_v1",
            "task_id": "task_test_123",
            "created_at_utc": "2026-02-22T00:00:00+00:00",
            "compiler_name": "task_compile_manager",
            "compiler_version": "v1",
            "source_task": "At 5 pm check for email from Katy, then text Jukka.",
            "source_information": "sms preferred",
            "source_hash": "abc123abc123abcd",
            "entry_step_id": "step_1",
            "steps": [
                {
                    "id": "step_1",
                    "kind": "action",
                    "title": "Register watch",
                    "executor": "emi_team_manager",
                    "instruction": "Register watch for email from Katy.",
                    "next_step": "step_2",
                },
                {
                    "id": "step_2",
                    "kind": "wait_for_event",
                    "title": "Wait for match",
                    "event_name": "signal_router.watch.email_katy",
                    "next_step": "step_3",
                },
                {
                    "id": "step_3",
                    "kind": "action",
                    "title": "Compose email draft",
                    "executor": "personal_admin_manager",
                    "instruction": "Compose an email draft summarizing the matched email.",
                    "next_step": "step_4",
                },
                {
                    "id": "step_4",
                    "kind": "action",
                    "title": "Send email",
                    "executor": "personal_admin_manager",
                    "instruction": "Send the drafted email to Jukka.",
                    "next_step": "step_5",
                },
                {
                    "id": "step_5",
                    "kind": "end",
                    "title": "Done",
                },
            ],
        }
        return {
            "what_i_am_thinking": "Task can be represented as workflow nodes.",
            "summary": "Compiled to workflow IR.",
            "checklist": ["parse intent", "emit workflow"],
            "progress": ["done"],
            "plan": "compile",
            "action": "return_control",
            "action_input": "{}",
            "phase_i_atoms": {
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "watch_event",
                        "title": "Email from Katy watch",
                        "description": "Watch for email from Katy.",
                        "canonical_event_name": "signal_router.watch.email_katy",
                    }
                ],
                "actions": [
                    {
                        "id": "action_1",
                        "executor": "personal_admin_manager",
                        "title": "Compose and send update",
                        "instruction": "Compose and send concise update to Jukka.",
                    }
                ],
            },
            "compiled_task": compiled_task,
            "logic_tree": {
                "entry_step_id": "step_1",
                "edges": [
                    {"from_step_id": "step_1", "to_step_id": "step_2", "reason": "next_step"},
                    {"from_step_id": "step_2", "to_step_id": "step_3", "reason": "next_step"},
                    {"from_step_id": "step_3", "to_step_id": "step_5", "reason": "merged_next"},
                ],
                "bindings": [
                    {"step_id": "step_2", "atomic_type": "event", "atomic_id": "event_1"},
                    {"step_id": "step_3", "atomic_type": "action", "atomic_id": "action_1"},
                ],
            },
            "final_answer_data_list": [
                {
                    "data_type": "compiled_task",
                    "key": "compiled_task",
                    "value": json.dumps(compiled_task),
                }
            ],
            "final_answer_answer": "Task compiled successfully.",
        }


def _build_request(task: str, info: str = "") -> Message:
    return Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task,
        information=info,
    )


def test_task_compile_manager_compiles_to_final_answer(monkeypatch):
    monkeypatch.setattr(
        "app.assistant.agent_runtime.services.llm_client.LLMFactory.get_llm_interface",
        lambda **kwargs: _StubLLMInterface(),
    )
    DI.manager_registry.preload_all()
    manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")

    result = manager.request_handler(
        _build_request(
            "At 5 pm check for email from Katy, then text Jukka.",
            "sms preferred",
        )
    )
    # Debug visibility: print full normalized manager payload for inspection.
    print("TASK_COMPILE_RESULT_DATA:")
    print(json.dumps(result.data, indent=2, ensure_ascii=False))

    assert result.result_type == "final_answer"
    assert isinstance(result.data, dict)
    assert "final_answer_answer" in result.data
    assert "Task compiled successfully." in result.data["final_answer_answer"]

    data_list = result.data.get("final_answer_data_list", [])
    assert isinstance(data_list, list)
    compiled = next((x for x in data_list if x.get("key") == "compiled_task"), None)
    assert compiled is not None
    if isinstance(compiled, dict) and isinstance(compiled.get("value"), str):
        print("COMPILED_TASK_PARSED:")
        parsed_compiled = json.loads(compiled["value"])
        print(json.dumps(parsed_compiled, indent=2, ensure_ascii=False))
        steps = parsed_compiled.get("steps", [])
        assert isinstance(steps, list)
        compose_send_steps = [
            step
            for step in steps
            if isinstance(step, dict)
            and step.get("kind") == "action"
            and step.get("executor") == "personal_admin_manager"
        ]
        assert len(compose_send_steps) == 1
        merged_step = compose_send_steps[0]
        assert "Compose email draft" in str(merged_step.get("title", ""))
        assert "Send email" in str(merged_step.get("title", ""))
        assert "Then:" in str(merged_step.get("instruction", ""))

        print("COMPILED_TASK_STEPS:")
        for index, step in enumerate(parsed_compiled.get("steps", []), start=1):
            title = step.get("title", "")
            kind = step.get("kind", "")
            instruction = step.get("instruction", "")
            event_name = step.get("event_name", "")
            detail = instruction or event_name
            print(f"  {index}) {title} [{kind}] - {detail}")


def test_task_compile_manager_injects_compile_seed_and_routes(monkeypatch):
    monkeypatch.setattr(
        "app.assistant.agent_runtime.services.llm_client.LLMFactory.get_llm_interface",
        lambda **kwargs: _StubLLMInterface(),
    )
    DI.manager_registry.preload_all()
    manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")

    _ = manager.request_handler(
        _build_request("Wait for email from Katy and then summarize.")
    )

    compile_seed = manager.blackboard.get_state_value("compile_seed")
    assert isinstance(compile_seed, dict)
    assert isinstance(compile_seed.get("task_id"), str)
    assert str(compile_seed.get("task_id", "")).startswith("task_")
    assert compile_seed.get("compiler_name") == "task_compile_manager"
    assert compile_seed.get("compiler_version") == "v1"
    assert isinstance(compile_seed.get("source_hash"), str)
    assert len(compile_seed["source_hash"]) == 16

    route_trace = manager.blackboard.get_state_value("manager_route_trace", [])
    assert isinstance(route_trace, list)
    routed_to_helper = any(item.get("next_agent") == "task_compile_metadata_node" for item in route_trace)
    routed_to_phase_i = any(item.get("next_agent") == "task_compile::phase_i" for item in route_trace)
    routed_to_phase_ii = any(item.get("next_agent") == "task_compile::phase_ii" for item in route_trace)
    routed_to_post = any(item.get("next_agent") == "task_compile_post_node" for item in route_trace)
    assert routed_to_helper
    assert routed_to_phase_i
    assert routed_to_phase_ii
    assert routed_to_post


class _TwoPhaseQualityStub:
    def __init__(self):
        self.phase_i_seen = False
        self.phase_ii_seen = False

    def structured_output(self, messages, use_json=False, **params):
        _ = use_json
        _ = params
        prompt_blob = "\n".join(
            str(msg.get("content") or "")
            for msg in messages
            if isinstance(msg, dict) and isinstance(msg.get("content"), str)
        )

        if "Phase I objective" in prompt_blob:
            self.phase_i_seen = True
            assert "Do not build full control flow yet." in prompt_blob
            assert "Do not encode branching logic in action instructions." in prompt_blob
            return {
                "what_i_am_thinking": "Decomposing task into atomic units.",
                "summary": "Extracted atomic events and action blocks.",
                "checklist": ["extract events", "extract actions"],
                "progress": ["phase_i_complete"],
                "plan": "phase_i",
                "action": "return_control",
                "action_input": "{}",
                "phase_i_atoms": {
                    "events": [
                        {
                            "id": "event_1",
                            "event_type": "watch_event",
                            "title": "Katy mention detected",
                            "description": "Mention of Katy in chat/email.",
                            "canonical_event_name": "signal_router.watch.katy_mention",
                        },
                        {
                            "id": "event_2",
                            "event_type": "time_event",
                            "title": "6PM local reached",
                            "description": "Phase transition boundary.",
                            "canonical_event_name": "clock.local.18_00",
                        },
                    ],
                    "actions": [
                        {
                            "id": "action_1",
                            "executor": "personal_admin_manager",
                            "title": "Notify Katy",
                            "instruction": "Send notification email to Katy.",
                        },
                        {
                            "id": "action_2",
                            "executor": "personal_admin_manager",
                            "title": "Notify Jukka",
                            "instruction": "Send notification email to Jukka.",
                        },
                    ],
                },
                "final_answer_answer": "Phase I decomposition complete.",
            }

        if "Phase II objective" in prompt_blob:
            self.phase_ii_seen = True
            assert "Phase I atomic decomposition (authoritative input):" in prompt_blob
            assert "event_1" in prompt_blob
            assert "signal_router.watch.katy_mention" in prompt_blob
            assert "clock.local.18_00" in prompt_blob
            compiled_task = {
                "schema_version": "task_ir_v1",
                "task_id": "task_phase_ii_123",
                "created_at_utc": "2026-02-26T00:00:00+00:00",
                "compiler_name": "task_compile_manager",
                "compiler_version": "v1",
                "source_task": "Monitor Katy until 6 PM, then Jukka until midnight.",
                "source_information": "",
                "source_hash": "phaseiihash123456",
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "wait_for_event",
                        "title": "Wait for Katy mention",
                        "event_name": "signal_router.watch.katy_mention",
                        "next_step": "s2",
                    },
                    {
                        "id": "s2",
                        "kind": "action",
                        "title": "Notify Katy",
                        "executor": "personal_admin_manager",
                        "instruction": "Send notification email to Katy.",
                        "next_step": "s3",
                    },
                    {
                        "id": "s3",
                        "kind": "wait_for_event",
                        "title": "Wait for 6 PM boundary",
                        "event_name": "clock.local.18_00",
                        "next_step": "s4",
                    },
                    {
                        "id": "s4",
                        "kind": "end",
                        "title": "Done",
                    },
                ],
            }
            return {
                "what_i_am_thinking": "Composing final workflow from Phase I atoms.",
                "summary": "Composed deterministic task graph.",
                "checklist": ["compose graph", "validate wiring"],
                "progress": ["phase_ii_complete"],
                "plan": "phase_ii",
                "action": "return_control",
                "action_input": "{}",
                "compiled_task": compiled_task,
                "logic_tree": {
                    "entry_step_id": "s1",
                    "edges": [
                        {"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"},
                        {"from_step_id": "s2", "to_step_id": "s3", "reason": "on_true"},
                        {"from_step_id": "s2", "to_step_id": "s4", "reason": "on_false"},
                        {"from_step_id": "s3", "to_step_id": "s4", "reason": "next_step"},
                    ],
                    "bindings": [
                        {"step_id": "s1", "atomic_type": "event", "atomic_id": "event_1"},
                        {"step_id": "s3", "atomic_type": "action", "atomic_id": "action_1"},
                    ],
                },
                "final_answer_data_list": [
                    {
                        "data_type": "compiled_task",
                        "key": "compiled_task",
                        "value": json.dumps(compiled_task),
                    }
                ],
                "final_answer_answer": "Task compiled successfully from Phase I atoms.",
            }

        raise AssertionError("Two-phase compile stub could not identify prompt phase.")


def test_task_compile_two_phase_quality_contract(monkeypatch):
    stub = _TwoPhaseQualityStub()
    monkeypatch.setattr(
        "app.assistant.agent_runtime.services.llm_client.LLMFactory.get_llm_interface",
        lambda **kwargs: stub,
    )
    DI.manager_registry.preload_all()
    manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")

    result = manager.request_handler(
        _build_request(
            "Monitor Katy mentions until 6 PM, then monitor Jukka mentions until midnight.",
            "Katy is Jukka's wife.",
        )
    )
    assert stub.phase_i_seen
    assert stub.phase_ii_seen
    assert result.result_type == "final_answer"
    assert isinstance(result.data, dict)

    atoms = manager.blackboard.get_state_value("phase_i_atoms")
    assert isinstance(atoms, dict)
    events = atoms.get("events", [])
    actions = atoms.get("actions", [])
    assert isinstance(events, list) and len(events) >= 2
    assert isinstance(actions, list) and len(actions) >= 2
    event_types = {str(e.get("event_type")) for e in events if isinstance(e, dict)}
    assert "watch_event" in event_types
    assert "time_event" in event_types
    executors = {str(a.get("executor")) for a in actions if isinstance(a, dict)}
    assert executors == {"personal_admin_manager"}

    data_list = result.data.get("final_answer_data_list", [])
    assert isinstance(data_list, list)
    compiled_item = next((x for x in data_list if isinstance(x, dict) and x.get("key") == "compiled_task"), None)
    assert isinstance(compiled_item, dict)
    compiled_json = compiled_item.get("value")
    assert isinstance(compiled_json, str)
    compiled_task = json.loads(compiled_json)
    steps = compiled_task.get("steps", [])
    assert isinstance(steps, list)
    wait_events = [
        str(step.get("event_name"))
        for step in steps
        if isinstance(step, dict) and step.get("kind") == "wait_for_event"
    ]
    assert "signal_router.watch.katy_mention" in wait_events
    assert "clock.local.18_00" in wait_events
