from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from app.assistant.routine_manager.utils import read_json_file, resources_dir, utc_now, write_json_file
from app.assistant.utils.time_utils import get_local_time


@dataclass
class DayFlowContext:
    pipeline_id: str
    run_id: str
    pipeline_config: Dict[str, Any]
    state_path: Path
    now_utc: Any
    now_local: Any
    boundary_date_local: str
    resources_dir: Path
    day_dir: Path
    pipeline_runs_dir: Path
    state: Dict[str, Any]

    @classmethod
    def build(
        cls,
        *,
        pipeline_id: str,
        run_id: str,
        pipeline_config: Dict[str, Any],
        state_path: Path,
    ) -> "DayFlowContext":
        from app.assistant.utils.time_utils import utc_to_local
        from app.assistant.utils.path_utils import get_configs_dir

        def _repo_root_dir() -> Path:
            return resources_dir().parent

        def _boundary_hour_local() -> int:
            try:
                cfg = read_json_file(get_configs_dir() / "dayflow_pipeline.json") or {}
                daily_reset = cfg.get("daily_reset") or {}
                return int(daily_reset.get("boundary_hour_local", 5))
            except Exception:
                return 5

        def boundary_date_local_str(now_local_dt) -> str:
            boundary_hour = _boundary_hour_local()
            effective_day = now_local_dt
            if now_local_dt.hour < boundary_hour:
                effective_day = now_local_dt - timedelta(days=1)
            return effective_day.strftime("%Y-%m-%d")

        def _day_context_dir(boundary_date_local: str) -> Path:
            year = boundary_date_local[:4]
            month = boundary_date_local[5:7]
            return _repo_root_dir() / "day_context" / year / month / boundary_date_local

        now_utc = utc_now()
        now_local = utc_to_local(now_utc)
        bdate = boundary_date_local_str(now_local)
        day_dir = _day_context_dir(bdate)
        pipeline_runs_dir = day_dir / "pipeline_runs"

        return cls(
            pipeline_id=pipeline_id,
            run_id=run_id,
            pipeline_config=pipeline_config,
            state_path=state_path,
            now_utc=now_utc,
            now_local=now_local,
            boundary_date_local=bdate,
            # Latest pipeline outputs live under resources/dayflow_pipeline_outputs/
            # (resources/ root is reserved for organized subfolders).
            resources_dir=resources_dir() / "dayflow_pipeline_outputs",
            day_dir=day_dir,
            pipeline_runs_dir=pipeline_runs_dir,
            state={},
        )

    def audit_path(self) -> Path:
        return self.pipeline_runs_dir / f"{self.pipeline_id}_{self.run_id}.json"

    def _default_state(self) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "last_updated_utc": utc_now().isoformat(),
            "boundary_date_local": None,
            "last_daily_reset_utc": None,
            "last_daily_reset_boundary": None,
            "day_started": False,
            "day_start_time_utc": None,
            "day_start_time": None,
            "wake_time_today": None,
            "user_is_up": None,
            "cursors": {"chat_last_seen_utc": None},
            "step_runs": {},
            "signals": {},
        }

    def load_state(self) -> None:
        loaded = read_json_file(self.state_path)
        base = self._default_state()
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                if k in base:
                    base[k] = v
            if isinstance(loaded.get("cursors"), dict):
                base["cursors"].update(loaded.get("cursors") or {})
            # Backward compatibility: migrate legacy `stage_runs` -> `step_runs` on load.
            if isinstance(loaded.get("step_runs"), dict):
                base["step_runs"].update(loaded.get("step_runs") or {})
            elif isinstance(loaded.get("stage_runs"), dict):
                base["step_runs"].update(loaded.get("stage_runs") or {})
            if isinstance(loaded.get("signals"), dict):
                base["signals"].update(loaded.get("signals") or {})
        self.state = base

    def save_state(self) -> None:
        self.state["last_updated_utc"] = utc_now().isoformat()
        write_json_file(self.state_path, self.state)

    def ensure_boundary_state(self) -> bool:
        prev = self.state.get("boundary_date_local")
        cur = self.boundary_date_local
        crossed = prev != cur
        if crossed:
            self.state["boundary_date_local"] = cur
            self.state["day_started"] = False
            self.state["day_start_time_utc"] = None
            self.state["signals"] = {}
        return crossed

    def daily_reset_needed(self) -> bool:
        return self.state.get("last_daily_reset_boundary") != self.boundary_date_local

    def mark_daily_reset_done(self) -> None:
        self.state["last_daily_reset_utc"] = self.now_utc.replace(tzinfo=timezone.utc).isoformat()
        self.state["last_daily_reset_boundary"] = self.boundary_date_local

