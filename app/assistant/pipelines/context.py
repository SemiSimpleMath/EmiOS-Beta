from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from app.assistant.utils.time_utils import (
    get_local_time,
    get_local_timezone,
    local_to_utc,
)
from app.assistant.routine_manager.utils import read_json_file, resources_dir, utc_now
from app.assistant.utils.path_utils import get_configs_dir


def _repo_root_dir() -> Path:
    return resources_dir().parent


def _day_context_root_dir() -> Path:
    return _repo_root_dir() / "day_context"


def _day_context_dir(date_str: str) -> Path:
    year, month = date_str[:4], date_str[5:7]
    return _day_context_root_dir() / year / month / date_str


def _boundary_hour_local() -> int:
    try:
        cfg = read_json_file(get_configs_dir() / "dayflow_pipeline.json") or {}
        daily_reset = cfg.get("daily_reset") or {}
        return int(daily_reset.get("boundary_hour_local", 5))
    except Exception:
        return 5


def boundary_date_local_str(now_local: datetime) -> str:
    boundary_hour = _boundary_hour_local()
    effective_day = now_local
    if now_local.hour < boundary_hour:
        effective_day = now_local - timedelta(days=1)
    return effective_day.strftime("%Y-%m-%d")


def day_window_local(date_str: str) -> Tuple[datetime, datetime]:
    local_tz = get_local_timezone()
    boundary_hour = _boundary_hour_local()
    y, m, d = (int(x) for x in date_str.split("-"))
    start_local = datetime(y, m, d, boundary_hour, 0, 0, tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local


@dataclass(frozen=True)
class PipelineContext:
    """
    Generic date-based pipeline context aligned to the day boundary hour.
    """

    pipeline_id: str
    run_id: str
    date_str: str
    local_tz: object
    start_local: datetime
    end_local: datetime
    since_utc: datetime
    until_utc: datetime
    day_dir: Path
    snapshots_dir: Path
    pipeline_runs_dir: Path

    @classmethod
    def for_date(cls, *, pipeline_id: str, target_date: Optional[str] = None, run_id: Optional[str] = None) -> "PipelineContext":
        now_local = get_local_time()
        date_str = (target_date or boundary_date_local_str(now_local)).strip()
        rid = run_id or uuid.uuid4().hex[:8]

        start_local, end_local = day_window_local(date_str)
        since_utc = local_to_utc(start_local)
        until_utc = local_to_utc(end_local)

        day_dir = _day_context_dir(date_str)
        snapshots_dir = day_dir / "resource_snapshots"
        pipeline_runs_dir = day_dir / "pipeline_runs"

        return cls(
            pipeline_id=pipeline_id,
            run_id=rid,
            date_str=date_str,
            local_tz=get_local_timezone(),
            start_local=start_local,
            end_local=end_local,
            since_utc=since_utc,
            until_utc=until_utc,
            day_dir=day_dir,
            snapshots_dir=snapshots_dir,
            pipeline_runs_dir=pipeline_runs_dir,
        )

    def audit_path(self) -> Path:
        return self.pipeline_runs_dir / f"{self.run_id}.json"

    def now_utc_iso(self) -> str:
        return utc_now().isoformat()

