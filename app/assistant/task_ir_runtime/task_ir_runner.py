from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.task_ir_runtime.task_ir_actions import TaskIRActionExecutor
from app.assistant.task_ir_runtime.task_ir_conditions import TaskIRConditionEvaluator
from app.assistant.task_ir_runtime.task_ir_state import TaskIRStateManager
from app.assistant.task_ir_runtime.task_ir_steps import TaskIRSteps
from app.assistant.task_ir_runtime.task_ir_timers import TaskIRTimerManager
from app.assistant.task_ir_runtime.task_ir_validator import TaskIRValidator
from app.assistant.task_ir_runtime.task_ir_waits import TaskIRWaitManager
from app.assistant.task_ir_runtime.state_store import TaskIRStateStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)
_TASKIR_BANNER = ">" * 14


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskIRRunner:
    """
    Deterministic runner for Workflow IR `task_ir_v1`.
    Non-deterministic behavior is isolated to delegated manager actions.
    """

    def __init__(self, *, max_steps_per_tick: int = 64) -> None:
        self._max_steps_per_tick = max(1, int(max_steps_per_tick))
        self._store = TaskIRStateStore()
        self._validator = TaskIRValidator()
        self._steps = TaskIRSteps()
        self._state = TaskIRStateManager()
        self._conditions = TaskIRConditionEvaluator()
        self._waits = TaskIRWaitManager(
            evaluate_condition=lambda condition, context: self._conditions.evaluate_condition(
                condition=condition,
                context=context,
            )
        )
        self._timers = TaskIRTimerManager()
        self._actions = TaskIRActionExecutor(
            normalize_context=lambda context: self._state.normalize_context(context),
            apply_task_state_update=lambda context, state_update: self._state.apply_task_state_update(
                context=context,
                state_update=state_update,
            ),
            append_task_log=lambda context, event: self._state.append_task_log(context=context, event=event),
            apply_step_task_state_update=lambda step, context: self._apply_step_task_state_update(
                step=step,
                context=context,
            ),
        )
        self._event_subscription_ready = False

    def ensure_event_subscription(self) -> None:
        if self._event_subscription_ready:
            return
        DI.event_hub.register_event("signal_router_watch_match", self._on_signal_router_event)
        DI.event_hub.register_event(TaskIRTimerManager.TIMER_EVENT_TOPIC, self._on_task_ir_timer_event)
        self._event_subscription_ready = True

    def start_run(
        self,
        *,
        compiled_task: dict[str, Any],
        initial_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        task = self._validator.validate_compiled_task(compiled_task)
        run_id = f"tir_{uuid4().hex[:12]}"
        created_at = _now_iso()
        context = self._state.normalize_context(dict(initial_context or {}))
        self._state.apply_preloaded_task_state(task=task, context=context)
        self._store.create_run(
            run_id=run_id,
            task_id=str(task["task_id"]),
            status="running",
            source_task_file=None,
            compiled_task=task,
            context=context,
            # TODO(task-state): converge `context` toward a structured per-run
            # state shape, e.g.:
            # - task_state.facts (gate inputs / materialized predicates)
            # - task_state.artifacts (drafts/payloads produced by actions)
            # - task_state.flags (booleans like digest_sent)
            # - task_log (append-only step execution/evaluation history)
            # Keep current behavior unchanged for now.
            current_step_id=str(task["entry_step_id"]),
            waiting_event_name=None,
            created_at_utc=created_at,
            started_at_utc=created_at,
        )
        logger.info(
            "%s TASKIR START %s run_id=%s task_id=%s entry_step=%s source_task_file=%s",
            _TASKIR_BANNER,
            _TASKIR_BANNER,
            run_id,
            str(task["task_id"]),
            str(task["entry_step_id"]),
            "",
        )
        return self._tick(run_id=run_id)

    def resume_run(
        self,
        *,
        run_id: str,
        event_name: Optional[str] = None,
        event_payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        run = self._require_run(run_id)
        status = str(run["status"])
        if status in {"completed", "failed"}:
            logger.info(
                "%s TASKIR RESUME NO-OP %s run_id=%s status=%s",
                _TASKIR_BANNER,
                _TASKIR_BANNER,
                run_id,
                status,
            )
            return run
        if status != "waiting":
            raise ValueError(f"Run must be waiting before resume: {run_id}")

        waiting_event = str(run.get("waiting_event_name") or "").strip()
        supplied_event = str(event_name or waiting_event).strip()
        if not supplied_event:
            raise ValueError(f"Run is waiting but has no waiting_event_name: {run_id}")
        logger.info(
            "%s TASKIR RESUME %s run_id=%s event=%s waiting_event=%s",
            _TASKIR_BANNER,
            _TASKIR_BANNER,
            run_id,
            supplied_event,
            waiting_event,
        )

        task = self._validator.validate_compiled_task(run["compiled_task"])
        step = self._steps.get_step(task=task, step_id=str(run["current_step_id"]))
        step_kind = str(step.get("kind") or "").strip()
        if step_kind not in {"wait_for_event", "wait_gate"}:
            raise ValueError(f"Run {run_id} is not paused at a wait step (wait_for_event/wait_gate).")
        allowed_events = self._waits.wait_step_subscriptions(step=step)
        if supplied_event not in allowed_events:
            raise ValueError(
                f"Resume event mismatch for run={run_id}: allowed={allowed_events} supplied={supplied_event}"
            )
        next_step = str(step.get("next_step") or "").strip()
        if not next_step:
            raise ValueError(f"wait step missing next_step: {step.get('id')}")

        context = dict(run.get("context") or {})
        context = self._state.normalize_context(context)
        event_data = dict(event_payload or {})
        timer_fire = self._timers.evaluate_timer_event_if_due(
            context=context,
            step=step,
            event_name=supplied_event,
        )
        if timer_fire:
            self._state.record_observed_event(
                context=context,
                event_name=supplied_event,
                event_payload={"timer_fired": True},
            )
            self._state.apply_wait_event_fact_bindings(step=step, event_name=supplied_event, context=context)
        else:
            self._state.record_observed_event(context=context, event_name=supplied_event, event_payload=event_data)
            self._state.apply_wait_event_fact_bindings(step=step, event_name=supplied_event, context=context)
        self._state.refresh_time_facts(context=context)

        should_advance = self._waits.evaluate_wait_release(step=step, context=context, event_name=supplied_event)
        target_step = next_step if should_advance else str(step.get("id") or "").strip()
        target_status = "running" if should_advance else "waiting"
        target_waiting_event = None if should_advance else (allowed_events[0] if allowed_events else None)

        self._store.update_run(
            run_id=run_id,
            status=target_status,
            context=context,
            current_step_id=target_step,
            waiting_event_name=target_waiting_event,
            last_error=None,
            finished_at_utc=None,
        )
        if should_advance:
            self._store.clear_wait_subscriptions(run_id=run_id)
            self._timers.sync_local_clock_resume_timers(run_id=run_id, event_names=[])
        else:
            self._store.set_wait_subscriptions(run_id=run_id, event_names=allowed_events, created_at_utc=_now_iso())
            self._timers.sync_local_clock_resume_timers(
                run_id=run_id,
                event_names=allowed_events,
                context=context,
                step_id=str(step.get("id") or "").strip(),
            )
        if not should_advance:
            logger.info(
                "%s TASKIR RESUME WAITING %s run_id=%s step_id=%s wait_events=%s",
                _TASKIR_BANNER,
                _TASKIR_BANNER,
                run_id,
                str(step.get("id") or ""),
                allowed_events,
            )
            return self._require_run(run_id)
        return self._tick(run_id=run_id)

    def _tick(self, *, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        task = self._validator.validate_compiled_task(run["compiled_task"])
        steps_map = self._steps.build_steps_map(task["steps"])
        step_order = self._steps.build_step_order(task=task)
        total_steps = len(step_order)
        context = dict(run.get("context") or {})
        context = self._state.normalize_context(context)
        current_step_id = str(run["current_step_id"])
        logger.info(
            "%s TASKIR TICK %s run_id=%s task_id=%s start_step=%s max_steps=%s",
            _TASKIR_BANNER,
            _TASKIR_BANNER,
            run_id,
            str(task.get("task_id") or ""),
            current_step_id,
            self._max_steps_per_tick,
        )

        processed = 0
        try:
            while processed < self._max_steps_per_tick:
                processed += 1
                self._state.refresh_time_facts(context=context)
                step = steps_map.get(current_step_id)
                if not isinstance(step, dict):
                    raise ValueError(f"Unknown step id: {current_step_id}")

                kind = str(step.get("kind") or "").strip()
                step_number = step_order.get(current_step_id, 0)
                logger.info(
                    "TASKIR STEP START run_id=%s step=%s/%s step_id=%s kind=%s title=%s",
                    run_id,
                    step_number or "?",
                    total_steps,
                    current_step_id,
                    kind,
                    str(step.get("title") or "").strip(),
                )
                if kind in {"wait_for_event", "wait_gate"}:
                    self._prepare_wait_cycle_entry(run_id=run_id, step=step, context=context)

                if kind == "action":
                    current_step_id = self._handle_action_step(
                        run_id=run_id,
                        step=step,
                        context=context,
                        task=task,
                    )
                    continue
                if kind == "tool_sequence":
                    current_step_id = self._handle_tool_sequence_step(
                        run_id=run_id,
                        step=step,
                        context=context,
                        task=task,
                    )
                    continue
                if kind == "wait_for_event":
                    next_step_id, terminal_run = self._handle_wait_for_event_step(
                        run_id=run_id,
                        step=step,
                        context=context,
                        current_step_id=current_step_id,
                    )
                    if terminal_run is not None:
                        return terminal_run
                    if not next_step_id:
                        raise ValueError(f"wait_for_event handler returned empty next step for run {run_id}.")
                    current_step_id = next_step_id
                    continue
                if kind == "wait_gate":
                    next_step_id, terminal_run = self._handle_wait_gate_step(
                        run_id=run_id,
                        step=step,
                        context=context,
                        current_step_id=current_step_id,
                    )
                    if terminal_run is not None:
                        return terminal_run
                    if not next_step_id:
                        raise ValueError(f"wait_gate handler returned empty next step for run {run_id}.")
                    current_step_id = next_step_id
                    continue
                if kind == "decision":
                    current_step_id = self._handle_decision_step(run_id=run_id, step=step, context=context)
                    continue
                if kind == "end":
                    return self._handle_end_step(run_id=run_id, step=step, context=context, current_step_id=current_step_id)
                raise ValueError(f"Unsupported step kind: {kind}")
        except Exception as e:
            logger.error("TaskIRRunner tick failure run_id=%s step_id=%s: %s", run_id, current_step_id, e)
            logger.debug("TaskIRRunner tick exception details", exc_info=True)
            return self._fail_run(
                run_id=run_id,
                context=context,
                current_step_id=current_step_id,
                error=f"Tick failed: {e}",
            )

        return self._fail_run(
            run_id=run_id,
            context=context,
            current_step_id=current_step_id,
            error=f"Step budget exceeded ({self._max_steps_per_tick})",
        )

    # Stable test seam: keeps focused unit tests independent from action/state internals.
    def _normalize_context(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._state.normalize_context(context)

    # Stable test seam: validates manager-result materialization contract in isolation.
    def _normalize_manager_result_to_task_state_update(
        self,
        *,
        step: dict[str, Any],
        result: Any,
    ) -> Optional[dict[str, Any]]:
        return self._actions.normalize_manager_result_to_task_state_update(step=step, result=result)

    # Stable test seam: preloaded-state loader contract (including __resource_ref__) in isolation.
    def _apply_preloaded_task_state(self, *, task: dict[str, Any], context: dict[str, Any]) -> None:
        self._state.apply_preloaded_task_state(task=task, context=context)

    def _apply_step_task_state_update(self, *, step: dict[str, Any], context: dict[str, Any]) -> None:
        self._state.apply_step_task_state_update(
            step=step,
            context=context,
            apply_update=lambda update: self._state.apply_task_state_update(context=context, state_update=update),
        )

    def _resolve_decision_next_step(self, *, step: dict[str, Any], context: dict[str, Any]) -> str:
        condition = str(step.get("condition") or "").strip()
        if not condition:
            raise ValueError(f"Decision step missing condition: {step.get('id')}")
        decision = self._conditions.evaluate_condition(condition=condition, context=context)
        branch_key = "on_true" if decision else "on_false"
        target = str(step.get(branch_key) or "").strip()
        if not target:
            raise ValueError(f"Decision step missing {branch_key}: {step.get('id')}")
        logger.info(
            "TASKIR DECISION step_id=%s condition=%s decision=%s branch=%s target=%s",
            str(step.get("id") or ""),
            condition,
            decision,
            branch_key,
            target,
        )
        return target

    def _handle_action_step(
        self,
        *,
        run_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        task: dict[str, Any],
    ) -> str:
        self._actions.execute_action_step(step=step, context=context, task=task)
        next_step = str(step.get("next_step") or "").strip()
        if not next_step:
            raise ValueError(f"Action step missing next_step: {step.get('id')}")
        logger.info("TASKIR ACTION ADVANCE run_id=%s from=%s to=%s", run_id, str(step.get("id") or ""), next_step)
        return next_step

    def _handle_tool_sequence_step(
        self,
        *,
        run_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        task: dict[str, Any],
    ) -> str:
        from app.assistant.task_ir_runtime.task_ir_tool_sequence import ToolSequenceExecutor
        executor = ToolSequenceExecutor()
        executor.execute_tool_sequence_step(step=step, context=context, task=task)
        next_step = str(step.get("next_step") or "").strip()
        if not next_step:
            raise ValueError(f"tool_sequence step missing next_step: {step.get('id')}")
        logger.info("TASKIR TOOL_SEQUENCE ADVANCE run_id=%s from=%s to=%s", run_id, str(step.get("id") or ""), next_step)
        return next_step

    def _handle_wait_for_event_step(
        self,
        *,
        run_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        current_step_id: str,
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        event_name = str(step.get("event_name") or "").strip()
        if not event_name:
            raise ValueError(f"wait_for_event step missing event_name: {step.get('id')}")
        next_step = str(step.get("next_step") or "").strip()
        if not next_step:
            raise ValueError(f"wait_for_event step missing next_step: {step.get('id')}")
        clear_condition = str(step.get("clear_condition") or "").strip()
        if clear_condition and bool(self._conditions.evaluate_condition(condition=clear_condition, context=context)):
            logger.info(
                "TASKIR WAIT CLEAR run_id=%s step_id=%s event=%s next=%s",
                run_id,
                str(step.get("id") or ""),
                event_name,
                next_step,
            )
            return next_step, None
        self._store.update_run(
            run_id=run_id,
            status="waiting",
            context=context,
            current_step_id=current_step_id,
            waiting_event_name=event_name,
            last_error=None,
            finished_at_utc=None,
        )
        self._store.set_wait_subscriptions(run_id=run_id, event_names=[event_name], created_at_utc=_now_iso())
        self._timers.sync_local_clock_resume_timers(
            run_id=run_id,
            event_names=[event_name],
            context=context,
            step_id=str(step.get("id") or "").strip(),
        )
        logger.info(
            "TASKIR WAITING run_id=%s step_id=%s event=%s clear_condition=%s",
            run_id,
            str(step.get("id") or ""),
            event_name,
            clear_condition or "<none>",
        )
        return None, self._require_run(run_id)

    def _handle_wait_gate_step(
        self,
        *,
        run_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        current_step_id: str,
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        subscriptions = self._waits.wait_step_subscriptions(step=step)
        next_step = str(step.get("next_step") or "").strip()
        if not next_step:
            raise ValueError(f"wait_gate step missing next_step: {step.get('id')}")
        release_condition = str(step.get("release_condition") or "").strip()
        if not release_condition:
            raise ValueError(f"wait_gate step missing release_condition: {step.get('id')}")
        should_release = bool(self._conditions.evaluate_condition(condition=release_condition, context=context))
        if should_release:
            logger.info(
                "TASKIR WAIT_GATE CLEAR run_id=%s step_id=%s fired_event=%s next=%s",
                run_id,
                str(step.get("id") or ""),
                "<event_hub_resume>",
                next_step,
            )
            self._store.clear_wait_subscriptions(run_id=run_id)
            self._timers.sync_local_clock_resume_timers(run_id=run_id, event_names=[])
            return next_step, None
        self._store.update_run(
            run_id=run_id,
            status="waiting",
            context=context,
            current_step_id=current_step_id,
            waiting_event_name=subscriptions[0],
            last_error=None,
            finished_at_utc=None,
        )
        self._store.set_wait_subscriptions(run_id=run_id, event_names=subscriptions, created_at_utc=_now_iso())
        self._timers.sync_local_clock_resume_timers(
            run_id=run_id,
            event_names=subscriptions,
            context=context,
            step_id=str(step.get("id") or "").strip(),
        )
        logger.info(
            "TASKIR WAIT_GATE WAITING run_id=%s step_id=%s subscriptions=%s release_condition=%s",
            run_id,
            str(step.get("id") or ""),
            subscriptions,
            release_condition,
        )
        return None, self._require_run(run_id)

    def _handle_decision_step(self, *, run_id: str, step: dict[str, Any], context: dict[str, Any]) -> str:
        next_step_id = self._resolve_decision_next_step(step=step, context=context)
        logger.info("TASKIR DECISION ADVANCE run_id=%s next_step=%s", run_id, next_step_id)
        return next_step_id

    def _prepare_wait_cycle_entry(self, *, run_id: str, step: dict[str, Any], context: dict[str, Any]) -> None:
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            raise ValueError("wait step missing id for prepare_wait_cycle entry.")
        kind = str(step.get("kind") or "").strip()
        subscriptions = self._waits.wait_step_subscriptions(step=step)
        if kind == "wait_for_event":
            self._waits.ensure_wait_watch_registration(context=context, step=step, event_name=subscriptions[0])
        elif kind == "wait_gate":
            self._waits.ensure_wait_gate_watch_registrations(context=context, step=step, subscriptions=subscriptions)
        else:
            raise ValueError(f"Unsupported wait step kind for prepare_wait_cycle entry: {kind}")
        self._state.clear_observed_events_for_wait_cycle(context=context, event_names=subscriptions)
        self._state.clear_wait_cycle_timer_state(context=context, wait_step_id=step_id, event_names=subscriptions)
        logger.info(
            "TASKIR PREPARE WAIT CYCLE run_id=%s step_id=%s subscriptions=%s",
            run_id,
            step_id,
            subscriptions,
        )

    def _handle_end_step(
        self,
        *,
        run_id: str,
        step: dict[str, Any],
        context: dict[str, Any],
        current_step_id: str,
    ) -> dict[str, Any]:
        _ = step
        self._waits.cleanup_run_watch_registrations(context=context)
        self._store.update_run(
            run_id=run_id,
            status="completed",
            context=context,
            current_step_id=current_step_id,
            waiting_event_name=None,
            last_error=None,
            finished_at_utc=_now_iso(),
        )
        self._store.clear_wait_subscriptions(run_id=run_id)
        self._timers.sync_local_clock_resume_timers(run_id=run_id, event_names=[])
        logger.info(
            "%s TASKIR COMPLETED %s run_id=%s step_id=%s",
            _TASKIR_BANNER,
            _TASKIR_BANNER,
            run_id,
            current_step_id,
        )
        return self._require_run(run_id)

    def _on_signal_router_event(self, message: Message) -> None:
        self._resume_waiting_runs_for_event(message=message, source_topic="signal_router_watch_match")

    def _on_task_ir_timer_event(self, message: Message) -> None:
        self._resume_waiting_runs_for_event(message=message, source_topic=TaskIRTimerManager.TIMER_EVENT_TOPIC)

    def _resume_waiting_runs_for_event(self, *, message: Message, source_topic: str) -> None:
        try:
            data = message.data if isinstance(message.data, dict) else {}
            event_name = str(data.get("event_name") or "").strip()
            if not event_name:
                logger.error("TaskIRRunner event callback received payload without event_name.")
                logger.debug("TaskIRRunner bad event payload", exc_info=False)
                return
            target_run_id = str(data.get("run_id") or "").strip()
            if target_run_id:
                try:
                    self.resume_run(
                        run_id=target_run_id,
                        event_name=event_name,
                        event_payload=data,
                    )
                except Exception as e:
                    logger.error(
                        "TaskIRRunner failed to resume targeted run=%s from event=%s topic=%s: %s",
                        target_run_id,
                        event_name,
                        source_topic,
                        e,
                    )
                    logger.debug("TaskIRRunner targeted resume exception details", exc_info=True)
                return
            waiting_runs = self._store.list_waiting_runs_by_event(event_name)
            for run in waiting_runs:
                run_id = str(run.get("run_id") or "").strip()
                if not run_id:
                    continue
                try:
                    self.resume_run(
                        run_id=run_id,
                        event_name=event_name,
                        event_payload=data,
                    )
                except Exception as e:
                    logger.error("TaskIRRunner failed to resume run=%s from event=%s topic=%s: %s", run_id, event_name, source_topic, e)
                    logger.debug("TaskIRRunner resume exception details", exc_info=True)
        except Exception as e:
            logger.error("TaskIRRunner event handler failure for topic=%s: %s", source_topic, e)
            logger.debug("TaskIRRunner event handler exception details", exc_info=True)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"Task IR run not found: {run_id}")
        return run

    def _fail_run(
        self,
        *,
        run_id: str,
        context: dict[str, Any],
        current_step_id: str,
        error: str,
    ) -> dict[str, Any]:
        cleanup_error: Optional[str] = None
        try:
            self._waits.cleanup_run_watch_registrations(context=context)
        except Exception as e:
            cleanup_error = str(e)
            logger.error("TaskIRRunner watch cleanup during fail path failed: %s", e)
            logger.debug("TaskIRRunner fail cleanup exception details", exc_info=True)
        final_error = str(error)
        if cleanup_error:
            final_error = f"{final_error} | cleanup_error={cleanup_error}"
        self._store.update_run(
            run_id=run_id,
            status="failed",
            context=context,
            current_step_id=current_step_id,
            waiting_event_name=None,
            last_error=final_error,
            finished_at_utc=_now_iso(),
        )
        self._store.clear_wait_subscriptions(run_id=run_id)
        self._timers.sync_local_clock_resume_timers(run_id=run_id, event_names=[])
        logger.error(
            "%s TASKIR FAILED %s run_id=%s step_id=%s error=%s",
            _TASKIR_BANNER,
            _TASKIR_BANNER,
            run_id,
            current_step_id,
            final_error,
        )
        return self._require_run(run_id)
