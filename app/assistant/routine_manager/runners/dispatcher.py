from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from app.assistant.routine_manager.runners.types import RoutineRunner


@dataclass(frozen=True)
class RoutineRunnerDispatcher:
    runners: Dict[str, RoutineRunner]

    def get(self, runner_name: str) -> RoutineRunner:
        key = str(runner_name or "").strip().lower()
        if not key:
            raise ValueError("runner is required")
        runner = (self.runners or {}).get(key)
        if runner is None:
            raise ValueError(f"Unknown runner: {runner_name}")
        return runner

