from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, Optional

from app.assistant.routine_manager.utils import read_json_file, status_dir
from app.assistant.utils.path_utils import get_configs_dir
from app.assistant.utils.logging_config import get_logger

from .context import DayFlowContext
from .step_runner import DayFlowRunner, _load_step_class
from .utils.room_scope import resolve_room_scope

logger = get_logger(__name__)


class DayFlowPipeline:
    """
    Step-based DayFlow pipeline.

    - Uses persisted state file configured by configs/dayflow_pipeline.json (state_resource_file)
    - Uses pipeline config: configs/dayflow_pipeline.json
    """

    pipeline_id = "dayflow"

    def __init__(self) -> None:
        self._config_path = get_configs_dir() / "dayflow_pipeline.json"
        self._config_mtime: float | None = None

        self._pipeline_config: Dict = {}
        self._steps: list = []
        self._runner: DayFlowRunner | None = None

        self._reload_config(force=True)

    def _state_path(self) -> Path:
        filename = str(self._pipeline_config.get("state_resource_file") or "resource_dayflow_status.json")
        return status_dir() / filename

    def _reload_config(self, *, force: bool = False) -> bool:
        """
        Reload configs/dayflow_pipeline.json and rebuild steps if the file changed.

        This matters because PipelineRegistry constructs pipeline singletons; if the pipeline was
        instantiated while step imports were broken (e.g. renames), it could end up with 0 steps
        until restart. This reload lets it self-heal.
        """
        try:
            mtime = self._config_path.stat().st_mtime
        except Exception:
            mtime = None

        changed = force or (mtime is not None and mtime != self._config_mtime)
        if not changed:
            return False

        cfg = read_json_file(self._config_path) or {}
        self._pipeline_config = cfg if isinstance(cfg, dict) else {}
        # Fail closed: dayflow must be explicitly room-scoped.
        resolve_room_scope(pipeline_config=self._pipeline_config)
        self._config_mtime = mtime

        self._steps = self._load_steps_from_config(self._pipeline_config)
        self._runner = DayFlowRunner(steps=self._steps)

        logger.info("[dayflow] loaded %d step(s) from %s", len(self._steps), str(self._config_path))
        return True

    @staticmethod
    def _load_steps_from_config(cfg: Dict) -> list:
        steps = []
        for item in (cfg.get("steps") or []):
            try:
                step_id = (item.get("id") or "").strip()
                if not step_id or not item.get("enabled", True):
                    continue
                step_class_path = item.get("step_class")
                if not step_class_path:
                    continue
                cls = _load_step_class(step_class_path)
                step_obj = cls()
                if not getattr(step_obj, "step_id", ""):
                    setattr(step_obj, "step_id", step_id)
                steps.append(step_obj)
            except Exception as e:
                logger.error("Failed to init dayflow step from config item=%s: %s", item, e)
        return steps

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        only_steps: Optional[list[str]] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict:
        if target_date:
            logger.info("dayflow ignoring target_date=%s (now-driven)", target_date)
        if force:
            logger.info("dayflow ignoring force=True (not supported)")

        rid = (run_id or uuid.uuid4().hex[:8]).strip() or uuid.uuid4().hex[:8]

        # Reload config if it changed on disk (self-heal without restart).
        self._reload_config(force=False)
        if not self._steps:
            # If we still have no steps, log loudly so this is visible in routine runner output.
            logger.warning("[dayflow] no steps loaded; pipeline run will be a no-op")

        ctx = DayFlowContext.build(
            pipeline_id=self.pipeline_id,
            run_id=rid,
            pipeline_config=self._pipeline_config,
            state_path=self._state_path(),
        )
        assert self._runner is not None
        return self._runner.run_once(ctx, only_steps=only_steps)

