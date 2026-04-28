from __future__ import annotations

import threading
from typing import Any, Optional


class TaskIRStateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._wait_subscriptions: dict[str, list[str]] = {}

    def _ensure_tables(self) -> None:
        return None

    def create_run(
        self,
        *,
        run_id: str,
        task_id: str,
        status: str,
        source_task_file: Optional[str],
        compiled_task: dict[str, Any],
        context: dict[str, Any],
        current_step_id: str,
        waiting_event_name: Optional[str],
        created_at_utc: str,
        started_at_utc: str,
    ) -> None:
        with self._lock:
            self._runs[run_id] = {
                "run_id": run_id,
                "task_id": task_id,
                "status": status,
                "source_task_file": source_task_file,
                "compiled_task": compiled_task if isinstance(compiled_task, dict) else {},
                "context": context if isinstance(context, dict) else {},
                "current_step_id": current_step_id,
                "waiting_event_name": waiting_event_name,
                "last_error": None,
                "created_at_utc": created_at_utc,
                "started_at_utc": started_at_utc,
                "finished_at_utc": None,
            }
            self._wait_subscriptions.pop(run_id, None)

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        with self._lock:
            row = self._runs.get(run_id)
            if row is None:
                return None
            return dict(row)

    def update_run(
        self,
        *,
        run_id: str,
        status: str,
        context: dict[str, Any],
        current_step_id: str,
        waiting_event_name: Optional[str],
        last_error: Optional[str],
        finished_at_utc: Optional[str],
    ) -> None:
        with self._lock:
            row = self._runs.get(run_id)
            if row is None:
                raise ValueError(f"Task IR run not found: {run_id}")
            row["status"] = status
            row["context"] = context if isinstance(context, dict) else {}
            row["current_step_id"] = current_step_id
            row["waiting_event_name"] = waiting_event_name
            row["last_error"] = last_error
            row["finished_at_utc"] = finished_at_utc

    def set_wait_subscriptions(self, *, run_id: str, event_names: list[str], created_at_utc: str) -> None:
        _ = created_at_utc
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in event_names:
            value = str(raw or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        with self._lock:
            self._wait_subscriptions[run_id] = list(normalized)

    def clear_wait_subscriptions(self, *, run_id: str) -> None:
        with self._lock:
            self._wait_subscriptions.pop(run_id, None)

    def list_waiting_runs_by_event(self, event_name: str) -> list[dict[str, Any]]:
        event_name = str(event_name or "").strip()
        if not event_name:
            raise ValueError("event_name is required")
        with self._lock:
            matches: list[dict[str, Any]] = []
            for run in self._runs.values():
                if str(run.get("status") or "") != "waiting":
                    continue
                run_id = str(run.get("run_id") or "").strip()
                if not run_id:
                    continue
                subs = self._wait_subscriptions.get(run_id) or []
                waiting_event_name = str(run.get("waiting_event_name") or "").strip()
                if event_name in subs or waiting_event_name == event_name:
                    matches.append(dict(run))
            return matches
