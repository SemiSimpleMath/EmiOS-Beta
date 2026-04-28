from __future__ import annotations

from typing import Optional

from app.assistant.pipelines.daily_insights.pipeline import DailyInsightsPipeline


def resolve_pipeline(pipeline_id: str):
    pid = str(pipeline_id or "").strip().lower()
    if pid == "daily_insights":
        return DailyInsightsPipeline()
    return None

