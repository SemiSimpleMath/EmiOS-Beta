from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.signal_router.contracts import (
    EMAIL_SEMANTIC_MATCH_WATCH_TYPE,
    SignalEnvelope,
    WatchMatchEvent,
    WatchRegistration,
    WatchRegistrationRequest,
)
from app.assistant.signal_router.services.garbage_filter_service import GarbageFilterService
from app.assistant.signal_router.services.state_store import SignalRouterStateStore
from app.assistant.signal_router.services.watcher_agent_service import WatcherAgentService
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


class _BoundedSet:
    """Thread-safe ordered set with a fixed capacity. Evicts the oldest entry when full."""

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._data: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def add(self, key: str) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return
            self._data[key] = None
            if len(self._data) > self._max_size:
                self._data.popitem(last=False)


class SignalRouterService:
    """Watch-driven event emitter.

    Consumers register watches (keyword_contains, email_semantic_match, etc.).
    The gut (IngestService) dispatches each inbound IngestEnvelope here via
    ``handle_envelope``; the match pipeline runs: garbage filter → watcher
    agent → dedupe → emit WatchMatchEvent. Signal_router is a pure
    subscriber; it does not poll any source itself (that moved to the gut).
    """

    def __init__(self, *, emit_to_event_hub: bool = False):
        self.emit_to_event_hub = bool(emit_to_event_hub)
        self._lock = threading.RLock()
        self._watcher_service = WatcherAgentService()
        self._garbage_filter = GarbageFilterService()
        self._state_store = SignalRouterStateStore()
        self._watches: Dict[str, WatchRegistration] = {}
        self._seen_dedupe_keys: "_BoundedSet" = _BoundedSet(max_size=10_000)
        self._load_persisted_watches()
        self._cancel_orphaned_task_ir_watches()

    def _cancel_orphaned_task_ir_watches(self) -> None:
        """
        On startup: cancel any persisted task_ir:: watches whose run no longer
        exists in the (in-memory) task IR runner. Since the runner's state store
        is in-memory only, every task_ir:: watch from a previous process is orphaned.
        """
        try:
            cancelled = self._state_store.cancel_watches_by_key_prefix(prefix="task_ir::")
            if cancelled:
                # Also remove them from the in-memory dict
                with self._lock:
                    orphaned = [
                        rid for rid, w in self._watches.items()
                        if str(w.watch_key or "").startswith("task_ir::")
                    ]
                    for rid in orphaned:
                        del self._watches[rid]
                logger.info(
                    "SignalRouter startup: cancelled %d orphaned task_ir watches from previous process",
                    cancelled,
                )
            else:
                logger.debug("SignalRouter startup: no orphaned task_ir watches found")
        except Exception as e:
            logger.error("SignalRouter startup: failed to cancel orphaned task_ir watches: %s", e)
            logger.debug("SignalRouter orphan cleanup exception details", exc_info=True)

    def register_keyword_watch(
        self,
        *,
        watch_key: str,
        event_name: str,
        keywords: List[str],
        dedupe_window_seconds: int = 300,
    ) -> WatchRegistration:
        normalized_keywords = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
        if not normalized_keywords:
            raise ValueError("keywords must contain at least one non-empty string")
        with self._lock:
            for existing in self._watches.values():
                if existing.status != "active":
                    continue
                if existing.watch_key == watch_key and existing.event_name == event_name:
                    existing_keywords = existing.predicate.get("keywords") if isinstance(existing.predicate, dict) else None
                    if isinstance(existing_keywords, list) and sorted(existing_keywords) == sorted(normalized_keywords):
                        return existing
        request = WatchRegistrationRequest(
            watch_key=watch_key,
            event_name=event_name,
            watch_type="keyword_contains",
            predicate={"keywords": normalized_keywords},
            dedupe_window_seconds=dedupe_window_seconds,
            metadata={"created_by": "signal_router_bootstrap"},
        )
        registration = WatchRegistration.from_request(request)
        with self._lock:
            self._watches[registration.registration_id] = registration
        self._state_store.upsert_watch(registration)
        logger.info(
            "SignalRouter watch registered registration_id=%s watch_key=%s event_name=%s keywords=%s",
            registration.registration_id,
            registration.watch_key,
            registration.event_name,
            normalized_keywords,
        )
        return registration

    def register_watch(self, *, request: WatchRegistrationRequest) -> WatchRegistration:
        if not isinstance(request, WatchRegistrationRequest):
            raise ValueError("request must be WatchRegistrationRequest")
        request.validate()
        logger.info(
            "SignalRouter register_watch requested watch_key=%s event_name=%s watch_type=%s",
            request.watch_key,
            request.event_name,
            request.watch_type,
        )
        with self._lock:
            for existing in self._watches.values():
                if existing.status != "active":
                    continue
                if (
                    existing.watch_key == request.watch_key
                    and existing.event_name == request.event_name
                    and existing.watch_type == request.watch_type
                    and existing.predicate == request.predicate
                ):
                    logger.info(
                        "SignalRouter register_watch reusing existing registration_id=%s event_name=%s watch_key=%s",
                        existing.registration_id,
                        existing.event_name,
                        existing.watch_key,
                    )
                    return existing
        registration = WatchRegistration.from_request(request)
        with self._lock:
            self._watches[registration.registration_id] = registration
        self._state_store.upsert_watch(registration)
        logger.info(
            "SignalRouter watch registered registration_id=%s watch_key=%s event_name=%s watch_type=%s",
            registration.registration_id,
            registration.watch_key,
            registration.event_name,
            registration.watch_type,
        )
        return registration

    def cancel_watch(self, *, registration_id: str) -> bool:
        registration_id = str(registration_id or "").strip()
        if not registration_id:
            raise ValueError("registration_id is required")
        with self._lock:
            self._watches.pop(registration_id, None)
        self._state_store.set_watch_status(registration_id=registration_id, status="cancelled", not_found_ok=True)
        logger.info("SignalRouter watch cancelled registration_id=%s", registration_id)
        return True

    def register_email_semantic_watch(
        self,
        *,
        watch_key: str,
        event_name: str,
        semantic_query: str,
        sender_equals: Optional[str] = None,
        sender_contains: Optional[str] = None,
        subject_contains: Optional[str] = None,
        account_id_equals: Optional[str] = None,
        dedupe_window_seconds: int = 300,
    ) -> WatchRegistration:
        predicate: dict[str, str] = {
            "semantic_query": str(semantic_query or "").strip(),
        }
        if sender_equals is not None:
            predicate["sender_equals"] = str(sender_equals).strip()
        if sender_contains is not None:
            predicate["sender_contains"] = str(sender_contains).strip()
        if subject_contains is not None:
            predicate["subject_contains"] = str(subject_contains).strip()
        if account_id_equals is not None:
            predicate["account_id_equals"] = str(account_id_equals).strip()
        request = WatchRegistrationRequest(
            watch_key=str(watch_key or "").strip(),
            event_name=str(event_name or "").strip(),
            watch_type=EMAIL_SEMANTIC_MATCH_WATCH_TYPE,
            predicate=predicate,
            dedupe_window_seconds=dedupe_window_seconds,
            metadata={"created_by": "signal_router_runtime"},
        )
        return self.register_watch(request=request)

    def _load_persisted_watches(self) -> None:
        watches = self._state_store.list_active_watches()
        with self._lock:
            for watch in watches:
                self._watches[watch.registration_id] = watch

    def handle_envelope(self, envelope: SignalEnvelope) -> None:
        """Public entrypoint called by the gut (IngestService) when a new
        envelope is dispatched.

        Loads the current active watch set and runs the envelope through
        the match pipeline (garbage filter → watcher agent → dedupe →
        emit). Idempotent via per-(watch, signal_id) dedupe: re-delivering
        the same envelope never emits a duplicate event and — with the
        pre-check in ``_process_signal`` — never burns a second watcher
        LLM call either.
        """
        envelope.validate()
        with self._lock:
            watches = list(self._watches.values())
        self._process_signal(signal=envelope, watches=watches)

    def _process_signal(self, *, signal: SignalEnvelope, watches: List[WatchRegistration]) -> None:
        signal_dict = signal.to_dict()
        for watch in watches:
            # Pre-check: if we've already emitted for this (watch, signal_id)
            # pair, skip the watcher LLM call entirely. Nothing useful comes
            # of re-invoking it — the output would just be deduped again.
            dedupe_key = f"{watch.registration_id}:{signal.signal_id}"
            with self._lock:
                if dedupe_key in self._seen_dedupe_keys:
                    continue
                if self._state_store.has_dedupe_key(dedupe_key):
                    self._seen_dedupe_keys.add(dedupe_key)
                    continue

            filter_decision = self._garbage_filter.decide_for_watch(signal=signal, watch=watch)
            filter_decision.validate()
            logger.info(
                "SignalRouter watch prefilter registration_id=%s event_name=%s watch_type=%s action=%s reason=%s",
                watch.registration_id,
                watch.event_name,
                watch.watch_type,
                filter_decision.action,
                filter_decision.reason_code,
            )
            if filter_decision.action != "keep":
                continue

            evaluation = self._watcher_service.evaluate(
                watch_registration=watch,
                signal=signal_dict,
                filter_decision=filter_decision.to_dict(),
            )
            logger.info(
                "SignalRouter watcher evaluation registration_id=%s event_name=%s emit=%s confidence=%.3f reason=%s",
                watch.registration_id,
                watch.event_name,
                evaluation.should_emit_event,
                evaluation.confidence,
                evaluation.match_reason,
            )
            if not evaluation.should_emit_event:
                continue

            # Record the dedupe key now that we're about to emit.
            with self._lock:
                self._seen_dedupe_keys.add(dedupe_key)
                self._state_store.store_dedupe_key(
                    dedupe_key=dedupe_key,
                    registration_id=watch.registration_id,
                )

            match_event = WatchMatchEvent(
                event_id=f"evt_{watch.registration_id}_{signal.signal_id}",
                event_name=watch.event_name,
                registration_id=watch.registration_id,
                watch_key=watch.watch_key,
                dedupe_key=dedupe_key,
                occurred_at_utc=signal.occurred_at_utc,
                source_signal_id=signal.signal_id,
                evidence=evaluation.evidence,
                metadata={
                    "confidence": evaluation.confidence,
                    "match_reason": evaluation.match_reason,
                    "proposed_payload": evaluation.proposed_payload,
                    "source_type": signal.source_type,
                    "source_id": signal.source_id,
                    "source_signal_type": signal.signal_type,
                    "source_metadata": signal.metadata,
                },
            )
            self._emit_match_event(match_event)

    def _emit_match_event(self, match_event: WatchMatchEvent) -> None:
        match_event.validate()
        if self.emit_to_event_hub:
            DI.event_hub.publish(
                Message(
                    sender="signal_router",
                    receiver=None,
                    data_type="signal_router_event",
                    content=match_event.event_name,
                    data=match_event.to_dict(),
                    event_topic="signal_router_watch_match",
                )
            )
            logger.info(
                "[SignalRouter EMIT] event_name=%s registration_id=%s signal_id=%s",
                match_event.event_name,
                match_event.registration_id,
                match_event.source_signal_id,
            )
            return

        logger.info(
            "[SignalRouter TEST EVENT] event_name=%s registration_id=%s signal_id=%s",
            match_event.event_name,
            match_event.registration_id,
            match_event.source_signal_id,
        )
