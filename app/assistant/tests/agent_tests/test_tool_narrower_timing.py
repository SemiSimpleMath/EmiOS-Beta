"""Test that the tool narrower completes in a reasonable time.

The tool narrower is called during task compilation to filter the full
tool catalog down to relevant tools. It should complete in under 15 seconds.
"""
import time

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


def test_tool_narrower_timing():
    """Tool narrower should complete in under 15 seconds."""
    from app.assistant.lib.task_utils.tool_planner import narrow_tools_for_task

    t0 = time.monotonic()
    result = narrow_tools_for_task(
        task_title="Morning Briefing",
        task_goal="Gather news screenshots, weather, and emails for a daily briefing.",
        step_descriptions=[
            "Go to cnn.com, take a screenshot, and scan for headlines.",
            "Go to bbc.com, take a screenshot, and scan for headlines.",
            "Get predicted weather for today.",
            "Retrieve emails from the last 10 hours.",
            "Provide gathered items to the daily summary manager.",
        ],
    )
    elapsed = time.monotonic() - t0

    print(f"\nTool narrower completed in {elapsed:.1f}s")
    print(f"Result: {len(result) if result else 'None'} tools selected")
    if result:
        print(f"Tools: {result}")

    assert elapsed < 15, f"Tool narrower took {elapsed:.1f}s — expected under 15s"
    assert result is not None, "Tool narrower returned None"
    assert len(result) > 0, "Tool narrower returned empty list"

    # Should include relevant tools
    result_set = set(result)
    assert "playwright_manager" in result_set or "web_navigate_snapshot" in result_set, \
        f"Expected browser tool in narrowed set: {result}"
    assert "get_weather" in result_set, f"Expected get_weather in narrowed set: {result}"


def test_tool_narrower_agent_direct():
    """Direct agent call should also be fast."""
    import json

    DI.tool_registry.load_tools()

    # Build candidate list (same as narrow_tools_for_task does)
    all_tools = sorted(DI.tool_registry.list_tools())
    candidates = []
    for name in all_tools:
        desc_obj = DI.tool_registry.get_tool_description_compact(name)
        description = ""
        if desc_obj is not None:
            description = str(getattr(desc_obj, "description", "") or "").strip()[:200]
        candidates.append({"name": name, "description": description})

    print(f"\nCandidate tools: {len(candidates)}")

    narrower = DI.agent_factory.create_agent("shared::tool_narrower")

    t0 = time.monotonic()
    result_msg = narrower.action_handler(
        Message(
            agent_input={
                "task": "Morning Briefing: Gather CNN/BBC screenshots, weather, and emails.",
                "information": "TaskIR step",
                "candidate_tools": json.dumps(candidates, ensure_ascii=False),
            }
        )
    )
    elapsed = time.monotonic() - t0

    result_data = getattr(result_msg, "data", None)
    likely = result_data.get("likely_tools", []) if isinstance(result_data, dict) else []

    print(f"Agent call completed in {elapsed:.1f}s")
    print(f"Selected {len(likely)} tools")
    if likely:
        print(f"Tools: {likely[:10]}...")

    assert elapsed < 15, f"Tool narrower agent took {elapsed:.1f}s — expected under 15s"


if __name__ == "__main__":
    test_tool_narrower_timing()
    test_tool_narrower_agent_direct()
    print("\nAll tests passed.")
