"""agent_input as a declared context item must reach the template (2026-06-12).

AgentInputApplier SPREADS a dict message.agent_input onto the blackboard
as individual keys, so the context injector's generic blackboard lookup
resolved the `agent_input` context item to None — every template using
{{ agent_input.field }} silently rendered BLANK and the agent judged
empty input (succession_judge, date_gap_gate, wiki fact agents,
answer_matcher all shipped on this trap). The injector now hands the
inbound message's agent_input through verbatim.

Both template conventions must work:
  - {{ agent_input.field }} with user_context_items: [agent_input]
  - {{ task }} / {{ information }} with the keys declared individually
"""
import os

os.environ["USE_TEST_DB"] = "true"
os.environ["TEST_DB_NAME"] = "test_agent_input_ctx"

import app.assistant.tests.test_setup  # noqa: F401

from unittest.mock import patch

from app.assistant.agent_runtime.services.llm_client import LLMClient
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


def _invoke_capturing_prompt(agent_name: str, agent_input: dict, fake_output: dict) -> str:
    captured = {}

    def spy(self, *, agent, messages, response_format=None, use_json=False):
        captured["messages"] = messages
        return fake_output

    agent = DI.agent_factory.create_agent(agent_name)
    assert agent is not None
    with patch.object(LLMClient, "call_structured_output", spy):
        agent.action_handler(Message(agent_input=agent_input))

    user_parts = [
        m.get("content") for m in captured.get("messages", [])
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    assert user_parts, "no user prompt captured"
    return "\n".join(user_parts)


def test_dict_agent_input_renders_via_agent_input_fields():
    prompt = _invoke_capturing_prompt(
        "kg_maintenance::succession_judge",
        {
            "label": "Sam's School",
            "category": "school",
            "start_date": "2020-09",
            "end_date": None,
            "today": "2026-06-12",
            "evidence": [{"date": "2024-03-01", "sentence": "Sam was dropped off at 7:45."}],
            "edges": ["Sam --attends--> Sam's School"],
        },
        {"verdict": "stable", "split_date": None, "split_basis": None,
         "era_before": None, "era_after": None, "confidence": "low",
         "reasoning": "test"},
    )
    assert "Sam's School" in prompt
    assert "Sam was dropped off at 7:45." in prompt
    assert "2024-03-01" in prompt


def test_spread_keys_still_render_for_individually_declared_items():
    prompt = _invoke_capturing_prompt(
        "context_enricher::planner",
        {"task": "Source: pod\nSummary: sensor alert xyzzy",
         "information": "- [Entity] **Quux Plugh** (match=0.99)"},
        {"enrichment": "test"},
    )
    assert "sensor alert xyzzy" in prompt
    assert "Quux Plugh" in prompt
