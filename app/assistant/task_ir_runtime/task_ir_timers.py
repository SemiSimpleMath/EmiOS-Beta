from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)
_TIMER_EVENT_PATTERN = re.compile(r"^clock\.timer\.(\d+)(s|m|h)$")
_LOCAL_CLOCK_EVENT_PATTERN = re.compile(r"^clock\.local\.(\d{2})_(\d{2})$")
_AFTER_CLOCK_EVENT_PATTERN = re.compile(r"^clock\.after\.(\d{2})_(\d{2})$")


class TaskIRTimerManager:
    TIMER_EVENT_TOPIC = "task_ir_timer_fired"

    def __init__(self) -> None:
        self._job_prefix = "taskir_timer"

    def evaluate_timer_event_if_due(
        self,
        *,
        context: dict[str, Any],
        step: dict[str, Any],
        event_name: str,
    ) -> bool:
        if self.is_clock_local_due(event_name=event_name):
            return True
        if self.is_clock_after_due(event_name=event_name):
            return True
        timer_seconds = self.parse_timer_event_seconds(event_name)
        if timer_seconds is None:
            return False
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            raise ValueError("wait_for_event timer step must include non-empty step id.")
        timer_key = f"{step_id}::{event_name}"
        timers = context.get("timers")
        if not isinstance(timers, dict):
            timers = {}
        timer_state = timers.get(timer_key)
        if not isinstance(timer_state, dict) or str(timer_state.get("event_name") or "") != event_name:
            armed_at = datetime.now(timezone.utc)
            timer_state = {
                "event_name": event_name,
                "timer_seconds": timer_seconds,
                "armed_at_utc": armed_at.isoformat(),
                "deadline_utc": (armed_at.timestamp() + timer_seconds),
            }
            timers[timer_key] = timer_state
            context["timers"] = timers
        deadline_utc = timer_state.get("deadline_utc")
        if not isinstance(deadline_utc, (int, float)):
            raise ValueError(f"Invalid timer deadline for step {step_id}: {deadline_utc!r}")
        now_epoch = datetime.now(timezone.utc).timestamp()
        due = now_epoch >= float(deadline_utc)
        if due:
            timers.pop(timer_key, None)
            context["timers"] = timers
        return due

    def parse_timer_event_seconds(self, event_name: str) -> Optional[int]:
        match = _TIMER_EVENT_PATTERN.match(event_name.strip())
        if not match:
            return None
        raw_value, raw_unit = match.groups()
        value = int(raw_value)
        if raw_unit == "s":
            return value
        if raw_unit == "m":
            return value * 60
        if raw_unit == "h":
            return value * 3600
        raise ValueError(f"Unsupported timer unit: {raw_unit}")

    def parse_local_clock_event(self, event_name: str) -> Optional[tuple[int, int]]:
        match = _LOCAL_CLOCK_EVENT_PATTERN.match(event_name.strip())
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour < 0 or hour > 23:
            raise ValueError(f"Unsupported local clock hour in event '{event_name}'.")
        if minute < 0 or minute > 59:
            raise ValueError(f"Unsupported local clock minute in event '{event_name}'.")
        return (hour, minute)

    def parse_after_clock_event(self, event_name: str) -> Optional[tuple[int, int]]:
        match = _AFTER_CLOCK_EVENT_PATTERN.match(event_name.strip())
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour < 0 or hour > 23:
            raise ValueError(f"Unsupported after-clock hour in event '{event_name}'.")
        if minute < 0 or minute > 59:
            raise ValueError(f"Unsupported after-clock minute in event '{event_name}'.")
        return (hour, minute)

    def is_clock_local_due(self, *, event_name: str) -> bool:
        parsed = self.parse_local_clock_event(event_name)
        if parsed is None:
            return False
        hour, minute = parsed
        now_local = datetime.now().astimezone()
        target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Fire only within a 60-second window after the crossing, not for the entire rest of the day.
        delta = (now_local - target_local).total_seconds()
        return 0 <= delta < 60

    def is_clock_after_due(self, *, event_name: str) -> bool:
        """Gate semantics: fires immediately if already past the target time today."""
        parsed = self.parse_after_clock_event(event_name)
        if parsed is None:
            return False
        hour, minute = parsed
        now_local = datetime.now().astimezone()
        target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now_local >= target_local

    def seconds_until_local_clock_event(self, *, event_name: str) -> Optional[float]:
        # Handles both clock.local.HH_MM (alarm) and clock.after.HH_MM (gate).
        parsed = self.parse_local_clock_event(event_name)
        is_after = False
        if parsed is None:
            parsed = self.parse_after_clock_event(event_name)
            is_after = True
        if parsed is None:
            return None
        hour, minute = parsed
        now_local = datetime.now().astimezone()
        target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_local <= now_local:
            if is_after:
                # Gate already open — fire immediately.
                return 0.5
            # Alarm already passed today — next crossing is tomorrow.
            target_local = target_local + timedelta(days=1)
        delta_seconds = (target_local - now_local).total_seconds()
        return max(0.0, delta_seconds)

    def sync_local_clock_resume_timers(
        self,
        *,
        run_id: str,
        event_names: list[str],
        context: Optional[dict[str, Any]] = None,
        step_id: Optional[str] = None,
    ) -> None:
        desired: dict[str, float] = {}
        for event_name in event_names:
            seconds = self.seconds_until_local_clock_event(event_name=event_name)
            if seconds is None:
                seconds = self._seconds_until_relative_timer_event(
                    event_name=event_name,
                    context=context,
                    step_id=step_id,
                )
            if seconds is None:
                continue
            timer_key = self._job_id(run_id=run_id, event_name=event_name)
            desired[timer_key] = seconds

        scheduler = self._require_scheduler()
        existing_keys = [job.id for job in scheduler.get_jobs() if str(job.id).startswith(f"{self._job_prefix}::{run_id}::")]
        for key in existing_keys:
            if key in desired:
                continue
            scheduler.remove_job(key)
        for key, delay_seconds in desired.items():
            if scheduler.get_job(key) is not None:
                continue
            _, _, event_name = key.split("::", 2)
            scheduler.add_job(
                func=self.on_local_clock_timer,
                trigger="date",
                run_date=datetime.now(timezone.utc) + timedelta(seconds=max(0.01, float(delay_seconds))),
                kwargs={"run_id": run_id, "event_name": event_name},
                id=key,
                misfire_grace_time=300,
                replace_existing=False,
            )
            logger.info(
                "TASKIR RESUME TIMER armed run_id=%s event=%s delay_seconds=%.3f",
                run_id,
                event_name,
                float(delay_seconds),
            )

    def on_local_clock_timer(self, *, run_id: str, event_name: str) -> None:
        try:
            DI.event_hub.publish(
                Message(
                    sender="task_ir_timer_manager",
                    receiver="task_ir_runner",
                    content="task ir timer fired",
                    data={"run_id": run_id, "event_name": event_name, "timer_fired": True},
                    event_topic=self.TIMER_EVENT_TOPIC,
                )
            )
        except Exception as e:
            logger.error(
                "TaskIRRunner timer event publish failed run_id=%s event=%s: %s",
                run_id,
                event_name,
                e,
            )
            logger.debug("TaskIRRunner timer event publish exception details", exc_info=True)

    def _seconds_until_relative_timer_event(
        self,
        *,
        event_name: str,
        context: Optional[dict[str, Any]],
        step_id: Optional[str],
    ) -> Optional[float]:
        timer_seconds = self.parse_timer_event_seconds(event_name)
        if timer_seconds is None:
            return None
        if not isinstance(context, dict):
            return float(timer_seconds)
        normalized_step_id = str(step_id or "").strip()
        if not normalized_step_id:
            return float(timer_seconds)

        timers = context.get("timers")
        if not isinstance(timers, dict):
            timers = {}
            context["timers"] = timers
        timer_key = f"{normalized_step_id}::{event_name}"
        timer_state = timers.get(timer_key)
        now_epoch = datetime.now(timezone.utc).timestamp()
        if not isinstance(timer_state, dict):
            deadline = now_epoch + float(timer_seconds)
            timers[timer_key] = {
                "event_name": event_name,
                "timer_seconds": timer_seconds,
                "armed_at_utc": datetime.now(timezone.utc).isoformat(),
                "deadline_utc": deadline,
            }
            context["timers"] = timers
            return float(timer_seconds)
        deadline = timer_state.get("deadline_utc")
        if not isinstance(deadline, (int, float)):
            raise ValueError(f"Invalid timer deadline for step {normalized_step_id}: {deadline!r}")
        return max(0.01, float(deadline) - now_epoch)

    def _job_id(self, *, run_id: str, event_name: str) -> str:
        return f"{self._job_prefix}::{run_id}::{event_name}"

    @staticmethod
    def _require_scheduler():
        scheduler_service = getattr(DI, "scheduler", None)
        if scheduler_service is None:
            raise RuntimeError("DI.scheduler is required for TaskIR timer scheduling.")
        timing_engine = getattr(scheduler_service, "timing_engine", None)
        if timing_engine is None:
            raise RuntimeError("DI.scheduler.timing_engine is required for TaskIR timer scheduling.")
        scheduler = getattr(timing_engine, "scheduler", None)
        if scheduler is None:
            raise RuntimeError("APScheduler instance is required for TaskIR timer scheduling.")
        return scheduler