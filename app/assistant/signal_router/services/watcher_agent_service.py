from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.signal_router.contracts import (
    WatchRegistration,
    WatcherAgentInput,
    WatcherAgentOutput,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_WATCHER_FAILURE_ALERT_THRESHOLD = 3


class WatcherAgentService:
    """
    Thin wrapper around the signal_router::watcher Agent.

    Invocation pattern intentionally follows single-agent usage:
      watcher = DI.agent_factory.create_agent("signal_router::watcher")
      watcher.action_handler(Message(agent_input=...))
    """

    AGENT_NAME = "signal_router::watcher"

    def __init__(self) -> None:
        self._failure_counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def evaluate(
        self,
        *,
        watch_registration: WatchRegistration,
        signal: Dict[str, Any],
        filter_decision: Optional[Dict[str, Any]] = None,
    ) -> WatcherAgentOutput:
        if not isinstance(watch_registration, WatchRegistration):
            raise ValueError("watch_registration must be WatchRegistration")
        if not isinstance(signal, dict) or not signal:
            raise ValueError("signal must be a non-empty dict")
        if filter_decision is not None and not isinstance(filter_decision, dict):
            raise ValueError("filter_decision must be a dict when provided")

        agent_input = WatcherAgentInput(
            watch_registration=watch_registration.to_dict(),
            signal=signal,
            filter_decision=filter_decision,
        )
        payload = agent_input.to_dict()

        try:
            watcher = DI.agent_factory.create_agent(self.AGENT_NAME)
            result_msg = watcher.action_handler(Message(agent_input=payload))
            result_data = result_msg.data if isinstance(getattr(result_msg, "data", None), dict) else {}
            if not result_data:
                raise ValueError("signal_router::watcher returned empty data")
        except Exception as e:
            logger.error("Watcher agent execution failed: %s", e)
            logger.debug("Watcher agent execution exception details", exc_info=True)
            self._record_failure(watch_registration.registration_id)
            return self._fallback_output(watch_registration=watch_registration, signal=signal, reason=f"watcher_error: {type(e).__name__}")

        self._clear_failures(watch_registration.registration_id)

        output = WatcherAgentOutput(
            should_emit_event=bool(result_data.get("should_emit_event", False)),
            match_reason=str(result_data.get("match_reason") or "").strip(),
            confidence=float(result_data.get("confidence", 0.0)),
            dedupe_key_hint=str(result_data.get("dedupe_key_hint") or "").strip(),
            evidence={
                "lines": [
                    str(item).strip()
                    for item in (result_data.get("evidence_lines") or [])
                    if str(item).strip()
                ]
            },
            proposed_payload={
                "summary": str(result_data.get("payload_summary") or "").strip()
            },
        )
        try:
            output.validate()
        except Exception as e:
            logger.error("Watcher output validation failed: %s", e)
            logger.debug("Watcher output validation exception details", exc_info=True)
            self._record_failure(watch_registration.registration_id)
            return self._fallback_output(
                watch_registration=watch_registration,
                signal=signal,
                reason=f"watcher_output_invalid: {type(e).__name__}",
            )
        logger.debug(
            "Watcher evaluation complete registration_id=%s emit=%s confidence=%.3f",
            watch_registration.registration_id,
            output.should_emit_event,
            output.confidence,
        )
        return output

    def _record_failure(self, registration_id: str) -> None:
        with self._lock:
            self._failure_counts[registration_id] += 1
            count = self._failure_counts[registration_id]
        if count >= _WATCHER_FAILURE_ALERT_THRESHOLD:
            logger.warning(
                "WatcherAgentService: %d consecutive failures for registration_id=%s — watcher may be broken",
                count,
                registration_id,
            )

    def _clear_failures(self, registration_id: str) -> None:
        with self._lock:
            self._failure_counts.pop(registration_id, None)

    @staticmethod
    def _fallback_output(*, watch_registration: WatchRegistration, signal: Dict[str, Any], reason: str) -> WatcherAgentOutput:
        signal_id = str(signal.get("signal_id") or "").strip() or "unknown_signal"
        dedupe_seed = f"{watch_registration.registration_id}:{signal_id}"
        dedupe_hint = hashlib.sha256(dedupe_seed.encode("utf-8")).hexdigest()[:24]
        return WatcherAgentOutput(
            should_emit_event=False,
            match_reason=str(reason).strip() or "watcher_fallback",
            confidence=0.0,
            dedupe_key_hint=dedupe_hint,
            evidence={"fallback": True},
            proposed_payload={},
        )
