from __future__ import annotations

from typing import Any, Callable

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.signal_router.contracts import WatchRegistrationRequest
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TaskIRWaitManager:
    def __init__(self, *, evaluate_condition: Callable[[str, dict[str, Any]], bool]) -> None:
        self._evaluate_condition = evaluate_condition

    def wait_step_subscriptions(self, *, step: dict[str, Any]) -> list[str]:
        kind = str(step.get("kind") or "").strip()
        if kind == "wait_for_event":
            event_name = str(step.get("event_name") or "").strip()
            if not event_name:
                raise ValueError(f"wait_for_event step missing event_name: {step.get('id')}")
            return [event_name]
        if kind == "wait_gate":
            raw = step.get("subscriptions")
            if not isinstance(raw, list) or not raw:
                raise ValueError(f"wait_gate step missing subscriptions: {step.get('id')}")
            out: list[str] = []
            seen: set[str] = set()
            for item in raw:
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                out.append(value)
            if not out:
                raise ValueError(f"wait_gate step has empty subscriptions after normalization: {step.get('id')}")
            return out
        raise ValueError(f"Unsupported wait step kind for subscriptions: {kind}")

    def evaluate_wait_release(self, *, step: dict[str, Any], context: dict[str, Any], event_name: str) -> bool:
        _ = event_name
        kind = str(step.get("kind") or "").strip()
        if kind == "wait_for_event":
            clear_condition = str(step.get("clear_condition") or "").strip()
            if clear_condition:
                return bool(self._evaluate_condition(clear_condition, context))
            return True
        if kind == "wait_gate":
            release_condition = str(step.get("release_condition") or "").strip()
            if not release_condition:
                raise ValueError(f"wait_gate step missing release_condition: {step.get('id')}")
            return bool(self._evaluate_condition(release_condition, context))
        raise ValueError(f"Unsupported wait step kind for release evaluation: {kind}")

    def ensure_wait_watch_registration(
        self,
        *,
        context: dict[str, Any],
        step: dict[str, Any],
        event_name: str,
    ) -> None:
        payload = step.get("watch_registration")
        if payload is None:
            logger.debug(
                "TaskIRRunner wait step has no watch_registration payload: step_id=%s event_name=%s",
                str(step.get("id") or "").strip(),
                event_name,
            )
            return
        if not isinstance(payload, dict):
            raise ValueError("wait_for_event.watch_registration must be an object when provided.")
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            raise ValueError("wait_for_event step missing id for watch registration.")
        registry = context.get("watch_registrations")
        if not isinstance(registry, dict):
            registry = {}
        if step_id in registry:
            existing = registry.get(step_id) if isinstance(registry.get(step_id), dict) else {}
            logger.info(
                "TaskIRRunner watch registration already exists; reusing step_id=%s event_name=%s registration_id=%s",
                step_id,
                event_name,
                str(existing.get("registration_id") or "").strip() or "unknown",
            )
            context["watch_registrations"] = registry
            return
        watch_type = str(payload.get("watch_type") or "").strip()
        if not watch_type:
            raise ValueError("wait_for_event.watch_registration.watch_type is required.")
        predicate = payload.get("predicate")
        if not isinstance(predicate, dict) or not predicate:
            raise ValueError("wait_for_event.watch_registration.predicate must be a non-empty object.")
        event_name_in_payload = str(payload.get("event_name") or "").strip()
        if event_name_in_payload and event_name_in_payload != event_name:
            raise ValueError(
                f"wait_for_event.watch_registration.event_name mismatch: '{event_name_in_payload}' != '{event_name}'"
            )
        watch_key = str(payload.get("watch_key") or "").strip() or f"task_ir::{step_id}::{event_name}"
        dedupe_window_seconds = int(payload.get("dedupe_window_seconds", 300))
        expires_at_utc = payload.get("expires_at_utc")
        if expires_at_utc is not None:
            expires_at_utc = str(expires_at_utc).strip()
            if not expires_at_utc:
                expires_at_utc = None
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("wait_for_event.watch_registration.metadata must be an object when provided.")
        logger.info(
            "TaskIRRunner registering wait watch step_id=%s event_name=%s watch_type=%s watch_key=%s",
            step_id,
            event_name,
            watch_type,
            watch_key,
        )
        request = WatchRegistrationRequest(
            watch_key=watch_key,
            event_name=event_name,
            watch_type=watch_type,
            predicate=dict(predicate),
            dedupe_window_seconds=dedupe_window_seconds,
            expires_at_utc=expires_at_utc,
            metadata=dict(metadata),
        )
        request.validate()
        signal_router = getattr(DI, "signal_router", None)
        if signal_router is None:
            raise RuntimeError("signal_router service is required for wait_for_event watch registration.")
        registration = signal_router.register_watch(request=request)
        registration_id = str(getattr(registration, "registration_id", "") or "").strip()
        if not registration_id:
            raise RuntimeError("signal_router.register_watch returned empty registration_id.")
        registry[step_id] = {
            "registration_id": registration_id,
            "event_name": event_name,
            "watch_type": watch_type,
            "watch_key": watch_key,
        }
        context["watch_registrations"] = registry
        logger.info(
            "TaskIRRunner watch registration success step_id=%s event_name=%s registration_id=%s",
            step_id,
            event_name,
            registration_id,
        )

    def ensure_wait_gate_watch_registrations(
        self,
        *,
        context: dict[str, Any],
        step: dict[str, Any],
        subscriptions: list[str],
    ) -> None:
        payload = step.get("watch_registrations")
        if payload is None:
            return
        if not isinstance(payload, list):
            raise ValueError("wait_gate.watch_registrations must be a list when provided.")
        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError("wait_gate.watch_registrations entries must be objects.")
            event_name = str(raw.get("event_name") or "").strip()
            if not event_name:
                raise ValueError("wait_gate.watch_registrations entry missing event_name.")
            if event_name not in subscriptions:
                raise ValueError(
                    f"wait_gate.watch_registrations event_name '{event_name}' not present in subscriptions."
                )
            self.ensure_wait_watch_registration_with_key(
                context=context,
                step=step,
                event_name=event_name,
                payload=raw,
                registry_key=f"{str(step.get('id') or '').strip()}::{event_name}",
            )

    def ensure_wait_watch_registration_with_key(
        self,
        *,
        context: dict[str, Any],
        step: dict[str, Any],
        event_name: str,
        payload: dict[str, Any],
        registry_key: str,
    ) -> None:
        _ = step
        if not isinstance(payload, dict):
            raise ValueError("wait watch registration payload must be object.")
        registry = context.get("watch_registrations")
        if not isinstance(registry, dict):
            registry = {}
        if registry_key in registry:
            context["watch_registrations"] = registry
            return
        watch_type = str(payload.get("watch_type") or "").strip()
        if not watch_type:
            raise ValueError("wait watch registration watch_type is required.")
        predicate = payload.get("predicate")
        if not isinstance(predicate, dict) or not predicate:
            raise ValueError("wait watch registration predicate must be a non-empty object.")
        payload_event_name = str(payload.get("event_name") or "").strip()
        if payload_event_name and payload_event_name != event_name:
            raise ValueError(f"wait watch registration event_name mismatch: '{payload_event_name}' != '{event_name}'")
        watch_key = str(payload.get("watch_key") or "").strip() or f"task_ir::{registry_key}"
        dedupe_window_seconds = int(payload.get("dedupe_window_seconds", 300))
        expires_at_utc = payload.get("expires_at_utc")
        if expires_at_utc is not None:
            expires_at_utc = str(expires_at_utc).strip() or None
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("wait watch registration metadata must be an object when provided.")
        request = WatchRegistrationRequest(
            watch_key=watch_key,
            event_name=event_name,
            watch_type=watch_type,
            predicate=dict(predicate),
            dedupe_window_seconds=dedupe_window_seconds,
            expires_at_utc=expires_at_utc,
            metadata=dict(metadata),
        )
        request.validate()
        signal_router = getattr(DI, "signal_router", None)
        if signal_router is None:
            raise RuntimeError("signal_router service is required for wait watch registration.")
        registration = signal_router.register_watch(request=request)
        registration_id = str(getattr(registration, "registration_id", "") or "").strip()
        if not registration_id:
            raise RuntimeError("signal_router.register_watch returned empty registration_id.")
        registry[registry_key] = {
            "registration_id": registration_id,
            "event_name": event_name,
            "watch_type": watch_type,
            "watch_key": watch_key,
        }
        context["watch_registrations"] = registry

    def cleanup_run_watch_registrations(self, *, context: dict[str, Any]) -> None:
        raw = context.get("watch_registrations")
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ValueError("context.watch_registrations must be an object when provided.")
        signal_router = getattr(DI, "signal_router", None)
        if signal_router is None:
            raise RuntimeError("signal_router service is required for watch cleanup.")
        for step_id, value in raw.items():
            if not isinstance(value, dict):
                raise ValueError(f"watch_registrations['{step_id}'] must be an object.")
            registration_id = str(value.get("registration_id") or "").strip()
            if not registration_id:
                raise ValueError(f"watch_registrations['{step_id}'].registration_id is required.")
            signal_router.cancel_watch(registration_id=registration_id)
            logger.info(
                "TaskIRRunner cancelled watch registration on run cleanup step_id=%s registration_id=%s",
                step_id,
                registration_id,
            )
        context["watch_registrations"] = {}