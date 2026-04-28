from __future__ import annotations

import uuid
from app.assistant.utils.logging_config import get_logger
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from app.assistant.routine_manager.utils import resources_dir

logger = get_logger(__name__)


def _week_dir(end_date_str: str) -> Path:
    """
    Week output directory: week_context/YYYY/WW/  where WW is ISO week number.
    e.g. week_context/2026/09/ for the week ending 2026-03-01
    """
    d = date.fromisoformat(end_date_str)
    iso_year, iso_week, _ = d.isocalendar()
    return resources_dir().parent / "week_context" / str(iso_year) / f"{iso_week:02d}"


class WeeklyPipelineContext:
    """
    Context for weekly pipeline runs. Mirrors the interface of PipelineContext
    so it can be passed to PipelineRunner without modifications.
    """

    def __init__(
        self,
        *,
        pipeline_id: str,
        run_id: str,
        end_date_str: str,
        num_days: int,
        week_dir: Path,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.run_id = run_id
        self.end_date_str = end_date_str
        self.num_days = num_days
        self.week_dir = week_dir
        self.pipeline_runs_dir = week_dir / "pipeline_runs"
        # Aliases expected by PipelineRunner
        self.day_dir = week_dir
        self.date_str = end_date_str

    @classmethod
    def for_week_ending(
        cls,
        *,
        pipeline_id: str,
        end_date_str: Optional[str] = None,
        num_days: int = 7,
        run_id: Optional[str] = None,
    ) -> "WeeklyPipelineContext":
        end = end_date_str or (date.today() - timedelta(days=1)).isoformat()
        rid = run_id or uuid.uuid4().hex[:8]
        wdir = _week_dir(end)
        return cls(
            pipeline_id=pipeline_id,
            run_id=rid,
            end_date_str=end,
            num_days=num_days,
            week_dir=wdir,
        )

    def audit_path(self) -> Path:
        return self.pipeline_runs_dir / f"{self.run_id}.json"
