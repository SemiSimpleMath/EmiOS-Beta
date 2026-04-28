from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.assistant.utils.path_utils import resolve_repo_path


class TaskIRStateManager:
    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def normalize_context(self, context: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(context, dict):
            raise TypeError(f"context must be dict, got {type(context)}")
        normalized = dict(context)
        task_state = normalized.get("task_state")
        if task_state is None:
            task_state = {}
        if not isinstance(task_state, dict):
            raise ValueError("context.task_state must be an object when provided.")
        for bucket in ("facts", "artifacts", "flags"):
            value = task_state.get(bucket)
            if value is None:
                task_state[bucket] = {}
                continue
            if not isinstance(value, dict):
                raise ValueError(f"context.task_state.{bucket} must be an object when provided.")
        data_index = task_state.get("data_index")
        if data_index is None:
            data_index = {}
        if not isinstance(data_index, dict):
            raise ValueError("context.task_state.data_index must be an object when provided.")
        task_state["data_index"] = data_index
        normalized["task_state"] = task_state
        task_log = normalized.get("task_log")
        if task_log is None:
            task_log = []
        if not isinstance(task_log, list):
            raise ValueError("context.task_log must be a list when provided.")
        normalized["task_log"] = task_log
        return normalized

    def apply_task_state_update(self, *, context: dict[str, Any], state_update: dict[str, Any]) -> None:
        normalized_context = self.normalize_context(context)
        task_state = normalized_context["task_state"]
        for bucket in ("facts", "artifacts", "flags"):
            update_value = state_update.get(bucket)
            if update_value is None:
                continue
            if not isinstance(update_value, dict):
                raise ValueError(f"task_state_update.{bucket} must be an object when provided.")
            task_state[bucket].update(update_value)

    def append_task_log(self, *, context: dict[str, Any], event: dict[str, Any]) -> None:
        normalized_context = self.normalize_context(context)
        if not isinstance(event, dict):
            raise ValueError("task_log event must be an object.")
        task_log = normalized_context["task_log"]
        task_log.append(dict(event))

    def apply_preloaded_task_state(self, *, task: dict[str, Any], context: dict[str, Any]) -> None:
        normalized_context = self.normalize_context(context)
        preloaded = task.get("preloaded_task_state")
        if preloaded is None:
            return
        if not isinstance(preloaded, dict):
            raise ValueError("compiled_task.preloaded_task_state must be an object when provided.")
        task_state = normalized_context["task_state"]
        for bucket in ("facts", "artifacts", "flags"):
            raw_bucket = preloaded.get(bucket)
            if raw_bucket is None:
                continue
            if not isinstance(raw_bucket, dict):
                raise ValueError(f"compiled_task.preloaded_task_state.{bucket} must be an object.")
            target_bucket = task_state.get(bucket)
            if not isinstance(target_bucket, dict):
                raise ValueError(f"context.task_state.{bucket} must be an object.")
            for key, value in raw_bucket.items():
                normalized_key = str(key or "").strip()
                if not normalized_key:
                    raise ValueError(
                        f"compiled_task.preloaded_task_state.{bucket} keys must be non-empty strings."
                    )
                if normalized_key in target_bucket and target_bucket[normalized_key] != value:
                    raise ValueError(
                        f"TaskIR preloaded state conflict in {bucket}.{normalized_key}: "
                        "initial_context and compiled_task define different values."
                    )
                target_bucket[normalized_key] = self.resolve_preloaded_bucket_value(
                    bucket=bucket,
                    key=normalized_key,
                    value=value,
                )

    def resolve_preloaded_bucket_value(self, *, bucket: str, key: str, value: Any) -> Any:
        if bucket != "artifacts" or not isinstance(value, dict):
            return value
        resource_ref = value.get("__resource_ref__")
        if not isinstance(resource_ref, dict):
            return value
        kind = str(resource_ref.get("kind") or "").strip()
        if kind != "repo_file":
            raise ValueError(f"compiled_task preloaded artifact '{key}' has unsupported resource_ref kind '{kind}'.")
        rel_path = str(resource_ref.get("path") or "").strip()
        if not rel_path:
            raise ValueError(f"compiled_task preloaded artifact '{key}' repo_file path is required.")
        resolved_path = resolve_repo_path(rel_path)
        if not resolved_path.exists() or not resolved_path.is_file():
            raise FileNotFoundError(f"compiled_task preloaded artifact '{key}' repo_file not found: {rel_path}")
        try:
            return resolved_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError(f"compiled_task preloaded artifact '{key}' repo_file must be UTF-8 text.") from exc

    def apply_step_task_state_update(
        self,
        *,
        step: dict[str, Any],
        context: dict[str, Any],
        apply_update: Callable[[dict[str, Any]], None],
    ) -> None:
        update = step.get("task_state_update")
        if update is None:
            return
        if not isinstance(update, dict):
            raise ValueError("step.task_state_update must be an object when provided.")
        apply_update(update)

    def apply_wait_event_fact_bindings(
        self,
        *,
        step: dict[str, Any],
        event_name: str,
        context: dict[str, Any],
    ) -> None:
        bindings = step.get("event_fact_bindings")
        if bindings is None:
            return
        if not isinstance(bindings, dict):
            raise ValueError("wait step event_fact_bindings must be an object when provided.")
        event_key = str(event_name or "").strip()
        if not event_key:
            raise ValueError("event_name is required for event_fact_bindings application.")
        selected = bindings.get(event_key, bindings.get("*"))
        if selected is None:
            return
        if not isinstance(selected, dict):
            raise ValueError("event_fact_bindings[event_name] must be an object.")
        normalized_context = self.normalize_context(context)
        task_state = normalized_context["task_state"]
        facts = task_state["facts"]
        for fact_key_raw, fact_value in selected.items():
            fact_key = str(fact_key_raw or "").strip()
            if not fact_key:
                raise ValueError("event_fact_bindings fact key must be non-empty.")
            if not fact_key.startswith("fact_"):
                raise ValueError(f"event_fact_bindings key '{fact_key}' must start with 'fact_'.")
            facts[fact_key] = fact_value

    def refresh_time_facts(self, *, context: dict[str, Any]) -> None:
        normalized = self.normalize_context(context)
        now = datetime.now().astimezone()
        time_obj = normalized.get("time")
        if not isinstance(time_obj, dict):
            time_obj = {}
        time_obj["local"] = now.strftime("%H:%M")
        time_obj["local_hhmm"] = now.strftime("%H:%M")
        time_obj["utc_iso"] = self._now_iso()
        normalized["time"] = time_obj
        task_state = normalized["task_state"]
        facts_obj = task_state["facts"]
        facts_obj["time_local_hhmm"] = time_obj["local_hhmm"]
        facts_obj["time_utc_iso"] = time_obj["utc_iso"]
        facts_obj["afk"] = self._read_afk_fact()

    @staticmethod
    def _read_afk_fact() -> bool:
        try:
            from app.assistant.ServiceLocator.service_locator import DI

            afk_monitor = getattr(DI, "afk_monitor", None)
            if afk_monitor is None:
                return False
            activity = afk_monitor.get_computer_activity()
            return bool(activity.get("is_afk", False))
        except Exception as e:
            import logging

            logging.getLogger(__name__).debug("TaskIRState: failed to read afk fact: %s", e, exc_info=True)
            return False

    def record_observed_event(self, *, context: dict[str, Any], event_name: str, event_payload: dict[str, Any]) -> None:
        normalized = self.normalize_context(context)
        events_obj = normalized.get("events")
        if not isinstance(events_obj, dict):
            events_obj = {}
        observed_obj = events_obj.get("observed")
        if not isinstance(observed_obj, dict):
            observed_obj = {}
        observed_obj[event_name] = True
        events_obj["observed"] = observed_obj
        events_obj["last_observed_event_name"] = event_name
        events_obj["last_observed_event_payload"] = dict(event_payload)
        events_obj["last_observed_event_at_utc"] = self._now_iso()
        normalized["events"] = events_obj

        history = normalized.get("event_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "event_name": event_name,
                "payload": dict(event_payload),
                "timestamp_utc": self._now_iso(),
            }
        )
        normalized["event_history"] = history
        task_state = normalized["task_state"]
        facts_obj = task_state["facts"]
        observed_facts = facts_obj.get("events_observed")
        if not isinstance(observed_facts, dict):
            observed_facts = {}
        observed_facts[event_name] = True
        facts_obj["events_observed"] = observed_facts
        self.append_task_log(
            context=normalized,
            event={
                "event_type": "event_observed",
                "event_name": event_name,
                "timestamp_utc": self._now_iso(),
            },
        )

    def clear_observed_events_for_wait_cycle(self, *, context: dict[str, Any], event_names: list[str]) -> None:
        normalized = self.normalize_context(context)
        if not isinstance(event_names, list):
            raise TypeError("event_names must be list[str].")
        normalized_names: list[str] = []
        seen: set[str] = set()
        for raw_name in event_names:
            if not isinstance(raw_name, str):
                raise TypeError("event_names entries must be str.")
            name = raw_name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized_names.append(name)
        if not normalized_names:
            return

        events_obj = normalized.get("events")
        if not isinstance(events_obj, dict):
            events_obj = {}
        observed_obj = events_obj.get("observed")
        if not isinstance(observed_obj, dict):
            observed_obj = {}
        for event_name in normalized_names:
            observed_obj.pop(event_name, None)
        events_obj["observed"] = observed_obj
        normalized["events"] = events_obj

        task_state = normalized["task_state"]
        facts_obj = task_state["facts"]
        observed_facts = facts_obj.get("events_observed")
        if not isinstance(observed_facts, dict):
            observed_facts = {}
        for event_name in normalized_names:
            observed_facts.pop(event_name, None)
        facts_obj["events_observed"] = observed_facts

    def clear_wait_cycle_timer_state(
        self,
        *,
        context: dict[str, Any],
        wait_step_id: str,
        event_names: list[str],
    ) -> None:
        normalized = self.normalize_context(context)
        step_id = str(wait_step_id or "").strip()
        if not step_id:
            raise ValueError("wait_step_id is required to clear wait cycle timer state.")
        if not isinstance(event_names, list):
            raise TypeError("event_names must be list[str].")
        timers = normalized.get("timers")
        if not isinstance(timers, dict):
            return
        seen: set[str] = set()
        for raw_name in event_names:
            if not isinstance(raw_name, str):
                raise TypeError("event_names entries must be str.")
            event_name = raw_name.strip()
            if not event_name or event_name in seen:
                continue
            seen.add(event_name)
            timer_key = f"{step_id}::{event_name}"
            timers.pop(timer_key, None)
        normalized["timers"] = timers