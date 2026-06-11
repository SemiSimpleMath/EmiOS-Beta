from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.routine_manager.utils import daily_insights_outputs_dir, read_json_file
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_timezone, get_local_time_str

from app.assistant.pipelines.context import PipelineContext


class ArchiveDailyInsightsStep:
    name = "archive_daily_insights"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [
            str(ctx.day_dir / "timeline_merged.json"),
            "agent: daily_timeline_insights",
            "configs/belief_domains.yaml (allowed tag vocabulary)",
        ]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "resource_daily_insights.json"]

    def run(self, ctx: PipelineContext) -> None:
        timeline_path = ctx.day_dir / "timeline_merged.json"
        if not timeline_path.exists():
            raise RuntimeError(f"Missing merged timeline: {timeline_path}")

        timeline_text = timeline_path.read_text(encoding="utf-8")
        # Tag vocabulary derives from the union of every enabled belief
        # domain's `tags` in configs/belief_domains.yaml. Adding a tag to a
        # domain there automatically lets this agent emit it.
        from belief_engine.config import list_all_tags
        allowed_tags = list_all_tags(only_enabled=True)

        agent = DI.agent_factory.create_agent("daily_timeline_insights")
        if agent is None:
            raise RuntimeError("daily_timeline_insights agent unavailable")
        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": "Extract actionable insights from the merged daily timeline.",
                "timeline_merged": timeline_text,
                "memory_available_tags": allowed_tags,
            }
        )
        result = agent.action_handler(msg)
        payload: dict[str, Any] = {}
        if hasattr(result, "data") and isinstance(result.data, dict):
            payload = result.data
        elif isinstance(result, dict):
            payload = result

        actionable = payload.get("actionable_information") if isinstance(payload, dict) else None
        if not isinstance(actionable, list):
            actionable = []

        filtered = []
        dropped_tags: list[dict[str, Any]] = []
        allowed_set = set(allowed_tags or [])
        for item in actionable:
            if not isinstance(item, dict):
                continue
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            keep_tags = [t for t in tags if t in allowed_set]
            dropped = [t for t in tags if t not in allowed_set]
            if dropped:
                dropped_tags.append({"fact_summary": item.get("fact_summary"), "dropped": dropped})
            item = dict(item)
            item["tags"] = keep_tags
            filtered.append(item)

        out_path = ctx.day_dir / "resource_daily_insights.json"
        out_payload = {
            "date": ctx.date_str,
            "timezone": str(get_local_timezone()),
            "source_timeline": str(timeline_path),
            "allowed_tags": allowed_tags,
            "actionable_information": filtered,
            "dropped_tags": dropped_tags,
        }
        write_json_atomic(out_path, out_payload)

        latest_payload = dict(out_payload)
        latest_payload["archive_path"] = str(out_path)
        write_json_atomic(daily_insights_outputs_dir() / "resource_daily_insights.json", latest_payload)

