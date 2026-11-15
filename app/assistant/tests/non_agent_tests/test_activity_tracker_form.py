"""The activity tracker's form and inputs (2026-11-15).

Its `activity_counts` was a Dict[str, int], which OpenAI's structured output refuses ("'properties' is required for
object schemas"), so every call failed; and the stage passed the chat as `recent_chat` while the prompt reads
`recent_chat_excerpts`, so the agent was always shown "No recent chat messages". Pins: counts are a list of
{activity, count}; no field is a free-form object; the input name the stage passes is the one the config declares.
"""
from pathlib import Path

import yaml

from app.assistant.agents.activity_tracker.agent_form import AgentForm
from app.assistant.utils.path_utils import get_app_root


def _objects_without_properties(schema, defs):
    for node in [schema, *defs.values()]:
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("type") == "object" and "properties" not in n:
                    yield n
                stack.extend(n.values())
            elif isinstance(n, list):
                stack.extend(n)


def test_counts_are_a_list_of_activity_and_count():
    answer = AgentForm.model_validate({"activity_counts": [{"activity": "hydration", "count": 2}],
                                       "activities_to_reset": ["hydration"], "reasoning": "drank water"})
    assert answer.activity_counts[0].activity == "hydration" and answer.activity_counts[0].count == 2
    schema = AgentForm.model_json_schema()
    assert list(_objects_without_properties(schema, schema.get("$defs", {}))) == []


def test_the_stage_passes_the_chat_under_the_name_the_prompt_reads():
    root = get_app_root()
    config = yaml.safe_load((root / "assistant/agents/activity_tracker/config.yaml").read_text(encoding="utf-8"))
    stage = Path(root / "assistant/pipelines/dayflow/steps/activity_tracker_stage.py").read_text(encoding="utf-8")
    assert "recent_chat_excerpts" in config["user_context_items"]
    assert '"recent_chat_excerpts": recent_chat' in stage
