import json

import app.assistant.tests.test_setup  # noqa: F401  (initialize DI)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message


class _StubLLMInterface:
    def __init__(self, result_dict: dict):
        self._result_dict = result_dict

    def structured_output(self, messages, use_json=False, **params):
        return self._result_dict


def test_multi_tool_planner_plan_only_emits_sequence_and_exits():
    bb = Blackboard()
    bb.update_state_value(
        "task",
        "Plan a sequence to capture screen 2 and 3, describe both, then write snippet.",
    )
    bb.update_state_value(
        "information",
        json.dumps(
            {
                "plan_only": True,
                "max_steps": 1,
            }
        ),
    )

    agent = DI.agent_factory.create_agent("multi_tool_agent::planner", blackboard=bb)
    assert agent is not None

    agent.llm_interface = _StubLLMInterface(
        {
            "plan_summary": "Capture and describe both screens, then write snippet.",
            "sequence": [
                {"id": "cap2", "tool_name": "capture_monitor_screenshot", "arguments_json": "{\"monitor_index\": 2}"},
                {
                    "id": "desc2",
                    "tool_name": "vision_image_describe",
                    "depends_on": ["cap2"],
                    "arguments_json": "{\"image_path\": \"{{steps.cap2.data.image_path}}\"}",
                },
                {"id": "cap3", "tool_name": "capture_monitor_screenshot", "arguments_json": "{\"monitor_index\": 3}"},
                {
                    "id": "desc3",
                    "tool_name": "vision_image_describe",
                    "depends_on": ["cap3"],
                    "arguments_json": "{\"image_path\": \"{{steps.cap3.data.image_path}}\"}",
                },
                {
                    "id": "write",
                    "tool_name": "write_text_file",
                    "depends_on": ["desc2", "desc3"],
                    "arguments_json": "{\"file_path\": \"out.md\", \"content\": \"snippet\"}",
                },
            ],
        }
    )

    result = agent.action_handler(Message(data_type="agent_activation"))
    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert result.get("mode") == "plan_only"
    assert isinstance(result.get("sequence"), list)
    assert len(result.get("sequence")) == 5
    assert result["sequence"][0]["tool_name"] == "capture_monitor_screenshot"
    assert bb.get_state_value("last_agent") == "multi_tool_agent::planner_return_control"
