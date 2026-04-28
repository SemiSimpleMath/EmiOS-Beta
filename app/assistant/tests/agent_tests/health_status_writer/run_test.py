"""
Standalone test: invoke health_status_writer agent directly.

Reads the same inputs the health_status stage feeds it (health beliefs from
resource_user_beliefs.json, today's sleep output, weekly insights flags) and
writes the generated markdown to:
  resources/dayflow_pipeline_outputs/resource_user_health_status.md

Run from repo root:
    python app/assistant/tests/agent_tests/health_status_writer/run_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[6]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.dayflow.steps.health_status_stage import (
    _format_health_beliefs,
    _format_weekly_health,
    _LATEST_FILENAME,
    _WEEKLY_INSIGHTS_PATH,
)
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time
from app.assistant.routine_manager.utils import resources_dir, read_json_file


def _read_sleep_output() -> str:
    data = read_json_file(resources_dir() / "dayflow_pipeline_outputs" / "resource_sleep_output.json")
    if not isinstance(data, dict):
        return "(sleep data unavailable)"
    parts = []
    summary = data.get("summary") or data.get("sleep_summary") or ""
    if summary:
        parts.append(str(summary))
    total_hours = data.get("total_sleep_hours") or data.get("total_hours")
    if total_hours is not None:
        parts.append(f"Total sleep: {total_hours}h")
    quality = data.get("quality") or data.get("sleep_quality") or ""
    if quality:
        parts.append(f"Quality: {quality}")
    debt = data.get("sleep_debt") or data.get("cumulative_debt")
    if debt is not None:
        parts.append(f"Sleep debt: {debt}")
    return "\n".join(parts) if parts else "(sleep data available but empty)"


def main() -> None:
    now_local = get_local_time()
    day_of_week = now_local.strftime("%A")
    date_time = now_local.strftime("%Y-%m-%d %H:%M")

    health_beliefs_block = _format_health_beliefs()
    sleep_output_block = _read_sleep_output()

    weekly_insights_data = None
    if _WEEKLY_INSIGHTS_PATH.exists():
        weekly_insights_data = read_json_file(_WEEKLY_INSIGHTS_PATH)
    weekly_health_block = _format_weekly_health(weekly_insights_data)

    scope = build_pipeline_scope_context(
        pipeline_id="dayflow",
        actor_id="health_status_test",
    )

    agent = DI.agent_factory.create_agent("health_status_writer")
    if agent is None:
        raise RuntimeError("health_status_writer agent not found — check config.yaml name field")

    msg = Message(
        scope_context=scope,
        agent_input={
            "date_time": date_time,
            "day_of_week": day_of_week,
            "health_beliefs_block": health_beliefs_block,
            "sleep_output_block": sleep_output_block,
            "weekly_health_block": weekly_health_block,
        },
    )

    print("=== health_status_writer test ===")
    print(f"Date: {date_time}  Day: {day_of_week}")
    print(f"Beliefs block length: {len(health_beliefs_block)} chars")
    print(f"Sleep block length:   {len(sleep_output_block)} chars")
    print(f"Weekly block length:  {len(weekly_health_block)} chars")
    print()

    result = agent.action_handler(msg)
    data = getattr(result, "data", None)

    if not isinstance(data, dict):
        raise RuntimeError(f"Agent returned unexpected result type: {type(result)}")

    markdown = data.get("markdown", "")

    print("=== generated health status ===")
    print(markdown)

    out_path = resources_dir() / "dayflow_pipeline_outputs" / _LATEST_FILENAME
    out_path.write_text(markdown, encoding="utf-8")
    print(f"\n=== Written to {out_path} ===")


if __name__ == "__main__":
    main()
