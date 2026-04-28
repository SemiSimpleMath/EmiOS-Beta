from __future__ import annotations

from typing import Any


class TaskIRSteps:
    @staticmethod
    def build_steps_map(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for step in steps:
            step_id = str(step.get("id") or "").strip()
            if not step_id:
                raise ValueError("compiled_task.steps requires non-empty step.id")
            if step_id in by_id:
                raise ValueError(f"compiled_task.steps has duplicate step.id: {step_id}")
            by_id[step_id] = step
        return by_id

    def build_step_order(self, *, task: dict[str, Any]) -> dict[str, int]:
        steps = task.get("steps")
        if not isinstance(steps, list):
            raise ValueError("compiled_task.steps must be a list")
        by_id = self.build_steps_map(steps)
        order: dict[str, int] = {}
        for idx, step in enumerate(steps):
            step_id = str(step.get("id") or "").strip()
            if step_id not in by_id:
                raise ValueError(f"compiled_task step id missing from map: {step_id}")
            order[step_id] = idx
        return order

    def get_step(self, *, task: dict[str, Any], step_id: str) -> dict[str, Any]:
        steps = task.get("steps")
        if not isinstance(steps, list):
            raise ValueError("compiled_task.steps must be a list")
        by_id = self.build_steps_map(steps)
        normalized_step_id = str(step_id or "").strip()
        if normalized_step_id not in by_id:
            raise ValueError(f"Unknown step id: {normalized_step_id}")
        return by_id[normalized_step_id]

    @staticmethod
    def normalized_step_data_ids(*, step: dict[str, Any], field_name: str) -> list[str]:
        raw = step.get(field_name)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError(f"Step '{step.get('id')}' field '{field_name}' must be list[str] when provided.")
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            value = str(item or "").strip()
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out