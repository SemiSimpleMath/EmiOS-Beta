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


def cite_sources(items: list[dict], entries: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """Each item with its cited entries stored as its provenance (`sources`: [{n, ref, entry}]), and what is
    wrong with the citations: an item citing nothing, or a number that is no entry of the day."""
    from belief_engine.intake.day_items import entry_ref
    out, problems = [], []
    for item in items:
        cited = list(dict.fromkeys(item.get("sources") or []))
        bad = [n for n in cited if n not in entries]
        if not cited:
            problems.append(f"insight {item.get('fact_summary')!r} cites no entries in sources")
        if bad:
            problems.append(f"insight {item.get('fact_summary')!r} cites {bad}, which are not entry numbers")
        out.append({**item, "sources": [{"n": n, "ref": entry_ref(entries[n]), "entry": entries[n]}
                                        for n in cited if n in entries]})
    return out, problems


class ArchiveDailyInsightsStep:
    name = "archive_daily_insights"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [
            str(ctx.day_dir / "timeline_merged.json"),
            "the assistant's replies that day (unified_log_2026)",
            "agent: daily_timeline_insights",
            "configs/belief_domains.yaml (allowed tag vocabulary)",
        ]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "resource_daily_insights.json"]

    def run(self, ctx: PipelineContext) -> None:
        timeline_path = ctx.day_dir / "timeline_merged.json"
        if not timeline_path.exists():
            raise RuntimeError(f"Missing merged timeline: {timeline_path}")

        # Every entry numbered once (the timeline plus the assistant's replies): the writer cites the entries
        # each insight rests on, and they are stored on the insight as its provenance, for the belief intake.
        from belief_engine.intake.day_items import day_entries
        doc, entries = day_entries(ctx.date_str)
        timeline_text = json.dumps(doc, ensure_ascii=False, indent=1)
        # Tag vocabulary derives from the union of every enabled belief
        # domain's `tags` in configs/belief_domains.yaml. Adding a tag to a
        # domain there automatically lets this agent emit it.
        from belief_engine.config import list_all_tags
        allowed_tags = list_all_tags(only_enabled=True)

        agent = DI.agent_factory.create_agent("daily_timeline_insights")
        if agent is None:
            raise RuntimeError("daily_timeline_insights agent unavailable")
        task = "Extract actionable insights from the merged daily timeline."

        def ask(task_text: str) -> list:
            result = agent.action_handler(Message(agent_input={
                "date_time": get_local_time_str(), "task": task_text,
                "timeline_merged": timeline_text, "memory_available_tags": allowed_tags}))
            payload = result.data if hasattr(result, "data") else result
            items = payload.get("actionable_information") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise RuntimeError("daily_timeline_insights returned no actionable_information list")
            return [i for i in items if isinstance(i, dict)]

        actionable, problems = cite_sources(ask(task), entries)
        if problems:
            actionable, problems = cite_sources(ask(f"{task} Correct your previous answer: {'; '.join(problems)}"),
                                                entries)
            if problems:
                raise RuntimeError(f"daily insights still cite no or unknown entries after one correction: {problems}")

        filtered = []
        dropped_tags: list[dict[str, Any]] = []
        allowed_set = set(allowed_tags or [])
        for item in actionable:
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

