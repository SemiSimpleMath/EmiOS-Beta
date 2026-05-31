"""
Standalone test: invoke dayflow_routine_writer agent directly.

Reads the same inputs the dayflow_routine stage feeds it (beliefs, daily context,
previous doc, weekly insights) and writes the generated markdown to:
  resources/dayflow_pipeline_outputs/resource_dayflow_routine.md

Run from repo root:
    python app/assistant/tests/agent_tests/dayflow_routine/run_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.dayflow.steps.dayflow_routine_stage import (
    _format_beliefs,
    _format_daily_context,
    _format_weekly_insights,
    _LATEST_FILENAME,
)
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time
from app.assistant.routine_manager.utils import read_json_file, resources_dir


def main() -> None:
    now_local = get_local_time()
    boundary_date_local = now_local.strftime("%Y-%m-%d")
    day_of_week = now_local.strftime("%A")
    date_time = now_local.strftime("%Y-%m-%d %H:%M")

    daily_ctx_path = (
        resources_dir().parent
        / "day_context"
        / boundary_date_local[:4]
        / boundary_date_local[5:7]
        / boundary_date_local
        / "resource_snapshots"
        / "resource_daily_context_generator_output.json"
    )
    daily_ctx_data = read_json_file(daily_ctx_path) if daily_ctx_path.exists() else None
    daily_context_block = _format_daily_context(daily_ctx_data)
    beliefs_block = _format_beliefs(day_of_week)
    weekly_insights_block = _format_weekly_insights()

    previous_doc_path = resources_dir() / "dayflow_pipeline_outputs" / _LATEST_FILENAME
    previous_doc = previous_doc_path.read_text(encoding="utf-8") if previous_doc_path.exists() else None

    scope = load_scope_for_source(
        kind="pipeline",
        source_id="dayflow",
        actor_id="dayflow_routine_test",
    )

    agent = DI.agent_factory.create_agent("dayflow_routine_writer")
    if agent is None:
        raise RuntimeError("dayflow_routine_writer agent not found — check config.yaml name field")

    msg = Message(
        scope_context=scope,
        agent_input={
            "date_time": date_time,
            "day_of_week": day_of_week,
            "boundary_date_local": boundary_date_local,
            "daily_context": daily_context_block,
            "beliefs_block": beliefs_block,
            "weekly_insights_block": weekly_insights_block,
            "previous_routine_doc": previous_doc or "",
        },
    )

    print("=== dayflow_routine_writer test ===")
    print(f"Date: {boundary_date_local}  Day: {day_of_week}  Time: {date_time}")
    print(f"Daily context available: {daily_ctx_data is not None}")
    print(f"Previous doc available:  {previous_doc is not None}")
    print(f"Beliefs block length:    {len(beliefs_block)} chars")
    print()

    result = agent.action_handler(msg)
    data = getattr(result, "data", None)

    if not isinstance(data, dict):
        raise RuntimeError(f"Agent returned unexpected result type: {type(result)}")

    markdown = data.get("markdown", "")
    change_summary = data.get("change_summary", "")

    print("=== change_summary ===")
    print(change_summary)
    print()
    print("=== generated markdown ===")
    print(markdown)

    out_path = resources_dir() / "dayflow_pipeline_outputs" / _LATEST_FILENAME
    out_path.write_text(markdown, encoding="utf-8")
    print(f"\n=== Written to {out_path} ===")


if __name__ == "__main__":
    main()
