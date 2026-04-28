"""
Integration test: given a rough, user-style task spec (intent only — no tool
names, no manager names, no produces/consumes), can the pre-compile step
(``tool_planner`` agent) infer the correct tool plan?

This is the "hard step" of compilation. Everything downstream (phase_i,
phase_ii, post-node) is mostly mechanical translation of pre-compile output.
If pre-compile can't turn user intent into the right (manager, tools,
produces, consumes) assignments, no later fix recovers it.

The rough spec here is a deliberately minimal version of morning_briefing —
numbered prose steps, nothing else. The test asserts that pre-compile
independently arrives at the same tool plan that the hand-crafted reference
spec explicitly specifies.

Run with:
    EMI_RUN_LLM_TESTS=1 .venv/Scripts/python.exe -m pytest \
        app/assistant/tests/test_precompile_from_rough_spec.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401

SKIP_LLM = os.environ.get("EMI_RUN_LLM_TESTS") != "1"
pytestmark = pytest.mark.skipif(
    SKIP_LLM,
    reason="Set EMI_RUN_LLM_TESTS=1 to run LLM-based pre-compile tests",
)


# ---------------------------------------------------------------------------
# Rough spec fixture — written as a user would actually type it.
# No manager names. No tool names. No produces/consumes. Just intent.
# ---------------------------------------------------------------------------

ROUGH_MORNING_BRIEFING = """## Title
Morning briefing

## Description
Pull together everything I need to start the day: headlines, weather,
important emails, and my current todos. Summarize it and save as JSON.

## Steps
1. Go to cnn.com and bbc.com, take a screenshot of each, and grab the top headlines.
2. Get today's weather forecast.
3. Get my important emails from the last 10 hours.
4. Get my current todo tasks.
5. Combine headlines, weather, emails, and todos into a daily briefing using the daily_summary agent.
6. Save the briefing as JSON to app/daily_summaries/daily_summary_today.json.

## Completion
The task completes when the briefing file is saved.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pre_compile(spec_markdown: str) -> list[dict]:
    """Run the tool_planner agent and return its step_plans list."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.lib.task_utils.tool_planner import (
        build_tool_catalog_for_prompt,
        narrow_tools_for_task,
    )
    from app.assistant.utils.pydantic_classes import Message

    narrowed = narrow_tools_for_task(
        task_title="",
        task_goal="",
        step_descriptions=[spec_markdown[:500]],
    )
    catalog_text = build_tool_catalog_for_prompt(only_tools=narrowed)
    agent = DI.agent_factory.create_agent("master_room::tool_planner")
    agent.blackboard.update_state_value("agent_input_catalog", catalog_text)
    result = agent.action_handler(Message(agent_input=spec_markdown))
    data = result.data or {}
    plans = data.get("step_plans") or []
    assert isinstance(plans, list) and plans, "tool_planner returned no step_plans"
    return plans


def _find_plan_where(plans: list[dict], predicate) -> dict | None:
    for p in plans:
        try:
            if predicate(p):
                return p
        except Exception:
            continue
    return None


def _plan_has_tool(plan: dict, tool_name_substr: str) -> bool:
    for t in plan.get("tools", []) or []:
        if isinstance(t, dict) and tool_name_substr in str(t.get("tool") or ""):
            return True
    return False


def _plan_instruction_mentions(plan: dict, *keywords: str) -> bool:
    hay = " ".join(str(plan.get(k) or "") for k in ("step_name", "instruction")).lower()
    return any(kw.lower() in hay for kw in keywords)


# ---------------------------------------------------------------------------
# Shared module fixture — run pre-compile once, exercise many assertions.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def step_plans() -> list[dict]:
    return _run_pre_compile(ROUGH_MORNING_BRIEFING)


# ---------------------------------------------------------------------------
# Tests — each covers one of the four dimensions (manager, tools, produces,
# consumes) plus determinism classification.
# ---------------------------------------------------------------------------

def test_rough_spec_yields_at_least_five_step_plans(step_plans):
    """Sanity: tool_planner should produce roughly one plan per spec step."""
    assert len(step_plans) >= 5, (
        f"Expected 5+ step plans from a 6-step rough spec, got {len(step_plans)}: "
        f"{[p.get('step_name') for p in step_plans]}"
    )


def test_browser_step_routed_to_playwright_manager(step_plans):
    """Step 1 (cnn/bbc) must be routed to playwright_manager as an action."""
    browser_plan = _find_plan_where(
        step_plans,
        lambda p: _plan_instruction_mentions(p, "cnn", "bbc", "headline"),
    )
    assert browser_plan is not None, (
        f"No step_plan matched cnn/bbc/headlines. Plans: "
        f"{[p.get('step_name') for p in step_plans]}"
    )
    assert browser_plan.get("manager_name") == "playwright_manager", (
        f"Browser step assigned manager={browser_plan.get('manager_name')!r}; "
        f"must be playwright_manager."
    )
    assert browser_plan.get("kind") == "action", (
        f"Browser step kind={browser_plan.get('kind')!r}; must be action "
        f"(LLM decides what to click/read)."
    )


def test_weather_step_is_deterministic_tool_call(step_plans):
    """Weather should be a tool_sequence calling get_weather."""
    weather_plan = _find_plan_where(
        step_plans,
        lambda p: _plan_instruction_mentions(p, "weather", "forecast"),
    )
    assert weather_plan is not None, "No step_plan for weather."
    assert weather_plan.get("kind") == "tool_sequence", (
        f"Weather step kind={weather_plan.get('kind')!r}; must be tool_sequence."
    )
    assert _plan_has_tool(weather_plan, "weather"), (
        f"Weather step has no weather-related tool: {weather_plan.get('tools')}"
    )


def test_email_step_uses_get_important_emails(step_plans):
    """Email retrieval should pick get_important_emails as a deterministic tool."""
    email_plan = _find_plan_where(
        step_plans,
        lambda p: _plan_instruction_mentions(p, "email"),
    )
    assert email_plan is not None, "No step_plan for emails."
    assert email_plan.get("kind") == "tool_sequence", (
        f"Email step kind={email_plan.get('kind')!r}; must be tool_sequence."
    )
    assert _plan_has_tool(email_plan, "get_important_emails") or _plan_has_tool(email_plan, "get_email"), (
        f"Email step has no email-retrieval tool: {email_plan.get('tools')}"
    )


def test_todo_step_uses_get_todo_tasks(step_plans):
    """Todo retrieval should pick get_todo_tasks as a deterministic tool."""
    todo_plan = _find_plan_where(
        step_plans,
        lambda p: _plan_instruction_mentions(p, "todo", "task list"),
    )
    assert todo_plan is not None, "No step_plan for todos."
    assert todo_plan.get("kind") == "tool_sequence", (
        f"Todo step kind={todo_plan.get('kind')!r}; must be tool_sequence."
    )
    assert _plan_has_tool(todo_plan, "get_todo_tasks"), (
        f"Todo step has no get_todo_tasks tool: {todo_plan.get('tools')}"
    )


def test_synthesis_step_consumes_all_gather_outputs(step_plans):
    """The synthesis step must consume artifacts from the four gather steps."""
    synthesis_plan = _find_plan_where(
        step_plans,
        lambda p: _plan_instruction_mentions(p, "daily_summary", "combine", "briefing") and len(p.get("consumes", []) or []) >= 3,
    )
    assert synthesis_plan is not None, (
        "No step_plan matches synthesis with 3+ consumed artifacts. "
        f"Plans: {[(p.get('step_name'), p.get('consumes')) for p in step_plans]}"
    )
    consumes = [c for c in (synthesis_plan.get("consumes") or []) if str(c).startswith("artifact_")]
    assert len(consumes) >= 3, (
        f"Synthesis step consumes only {consumes}; should pull at least 3 of the 4 "
        f"gather artifacts (headlines, weather, emails, todos)."
    )


def test_save_step_consumes_only_synthesis_artifact(step_plans):
    """The save step must consume exactly the synthesis output artifact (not raw gather data)."""
    save_plan = _find_plan_where(
        step_plans,
        lambda p: _plan_has_tool(p, "write_text_file") or _plan_has_tool(p, "save"),
    )
    assert save_plan is not None, "No save step_plan found."
    assert save_plan.get("kind") == "tool_sequence", (
        f"Save step kind={save_plan.get('kind')!r}; must be tool_sequence."
    )
    consumes = [c for c in (save_plan.get("consumes") or []) if str(c).startswith("artifact_")]
    assert len(consumes) == 1, (
        f"Save step consumes {consumes}; should consume exactly one artifact "
        f"(the synthesis output)."
    )


def test_produces_ids_chain_correctly(step_plans):
    """Every consumed artifact_* must be produced by some earlier plan."""
    produced: set[str] = set()
    for p in step_plans:
        for prod in p.get("produces", []) or []:
            if isinstance(prod, dict):
                pid = str(prod.get("id") or "")
                if pid:
                    produced.add(pid)

    orphans = []
    for p in step_plans:
        for cid in p.get("consumes", []) or []:
            if str(cid).startswith("artifact_") and cid not in produced:
                orphans.append((p.get("step_name"), cid))
    assert not orphans, (
        f"Orphan consumes (consumed but never produced by any plan): {orphans}"
    )


def test_pre_compile_output_is_inspectable(step_plans):
    """Dump the pre-compile output to the captured log so a human can sanity-check it.

    Doesn't assert — exists so the test run produces readable evidence of what
    pre-compile chose, even on an otherwise-green run.
    """
    print("\n--- pre-compile step_plans (raw) ---")
    print(json.dumps(step_plans, indent=2, ensure_ascii=False))
    print("--- end pre-compile step_plans ---\n")
