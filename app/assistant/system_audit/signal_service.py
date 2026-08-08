"""system_audit.signal_service — the friction ear on the gut.

Third IngestService subscriber (beside signal_router and pod_classifier).
Watches USER chat messages in the audit rooms (master_room + slack — owner
decision D1) for META-feedback about the assistant's own behavior ("this is
wrong", "why am I being asked again", "your question makes no sense") and
opens/attaches an id-bound case in the system audit register.

v1 classifies per envelope (one small luna call per user chat message in the
watched rooms). Sense wide, spend narrow: everything downstream (evidence
assembly, investigation) runs only per opened case.
"""
from __future__ import annotations

from typing import Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_WATCHED_ROOM_PREFIXES = ("master_room", "slack/")   # D1
_MIN_CONFIDENCE = 0.6
_MIN_CHARS = 4


class AuditSignalService:
    def __init__(self) -> None:
        self._scope = None

    # ------------------------------------------------------------------ gut
    def handle_envelope(self, envelope) -> None:
        try:
            if not self._is_watched_user_chat(envelope):
                return
            text = str(getattr(envelope, "content", "") or "").strip()
            if len(text) < _MIN_CHARS:
                return
            verdict = self._classify(text)
            if verdict is None or not verdict.get("friction"):
                return
            if float(verdict.get("confidence") or 0.0) < _MIN_CONFIDENCE:
                logger.info("[audit_signal] sub-threshold friction (%.2f) ignored: %r",
                            float(verdict.get("confidence") or 0.0), verdict.get("quote", "")[:60])
                return
            self._open_case(envelope, verdict)
        except Exception as e:
            # Contained per gut contract (a subscriber must not break the fan-out),
            # but LOUD — a silent audit ear defeats its purpose.
            logger.error("[audit_signal] envelope handling failed: %s", e, exc_info=True)

    # ------------------------------------------------------------ filtering
    @staticmethod
    def _is_watched_user_chat(envelope) -> bool:
        if str(getattr(envelope, "source_type", "")).strip() != "unified_log":
            return False
        meta = envelope.metadata if isinstance(getattr(envelope, "metadata", None), dict) else {}
        room = str(meta.get("room_id") or "").strip()
        if not room.startswith(_WATCHED_ROOM_PREFIXES):
            return False
        return str(meta.get("speaker_role") or "").strip().lower() == "user"

    # ---------------------------------------------------------- classifying
    def _classify(self, text: str) -> Optional[dict]:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message

        if self._scope is None:
            from app.assistant.scope.loader import load_scope_for_source
            self._scope = load_scope_for_source(
                kind="subsystem", source_id="system_audit",
                actor_id="audit_signal_service",
                identity_overrides={"surface": "internal",
                                    "scope_id": "system_audit::friction_classifier"},
            )
        agent = DI.agent_factory.create_agent("system_audit::friction_classifier")
        if agent is None:
            raise RuntimeError("audit_signal: friction classifier agent not found")
        result = agent.action_handler(Message(agent_input={"task": text},
                                              scope_context=self._scope))
        data = getattr(result, "data", None)
        return data if isinstance(data, dict) else None

    # -------------------------------------------------------------- opening
    @staticmethod
    def _open_case(envelope, verdict: dict) -> None:
        from app.assistant.system_audit import case_store
        from app.assistant.utils.time_utils import parse_iso_utc_strict

        meta = envelope.metadata if isinstance(envelope.metadata, dict) else {}
        room = str(meta.get("room_id") or "").strip() or None
        msg_id = str(getattr(envelope, "signal_id", "") or "").strip()
        quote = str(verdict.get("quote") or "").strip() or str(envelope.content or "").strip()
        kind = str(verdict.get("kind") or "other").strip()
        try:
            anchor = parse_iso_utc_strict(str(getattr(envelope, "occurred_at_utc", "") or ""))
        except Exception:
            anchor = None
        case_id = case_store.open_case(
            trigger_kind="user_friction",
            room_id=room,
            bound_ids={"message_ids": [msg_id]} if msg_id else {},
            summary=f"user friction ({kind}): {quote[:120]}",
            anchor_at=anchor,
            quote={"quote": quote, "message_id": msg_id, "kind": kind,
                   "at": str(getattr(envelope, "occurred_at_utc", "") or "")},
        )
        logger.info("[audit_signal] friction (%s) -> case %s", kind, case_id)
