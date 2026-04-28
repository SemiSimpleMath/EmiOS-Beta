from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4


SignalSourceType = Literal[
    "global_blackboard",
    "tool",
    "agent",
    "scheduler",
    "pipeline",
    "room",
    "system",
    "external",
]
EMAIL_SEMANTIC_MATCH_WATCH_TYPE = "email_semantic_match"

GarbageAction = Literal["keep", "drop"]
WatchStatus = Literal["active", "paused", "cancelled", "triggered", "expired"]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass
class SignalEnvelope:
    """
    Unified machine-readable signal consumed by SignalRouter.

    This should be produced from a single ingestion stream (for now: global blackboard
    records and other structured journal entries as they are added).
    """

    signal_id: str
    source_type: SignalSourceType
    source_id: str
    occurred_at_utc: str
    signal_type: str
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.signal_id = _require_non_empty(self.signal_id, "signal_id")
        self.source_id = _require_non_empty(self.source_id, "source_id")
        self.signal_type = _require_non_empty(self.signal_type, "signal_type")
        self.occurred_at_utc = _require_non_empty(self.occurred_at_utc, "occurred_at_utc")
        if not isinstance(self.data, dict):
            raise ValueError("data must be a dict")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class GarbageFilterDecision:
    """
    Deterministic filter output.

    Keep/drop decisions must be explicit and justified by reason_code.
    """

    action: GarbageAction
    reason_code: str
    details: str = ""
    normalized_content: str = ""
    tags: List[str] = field(default_factory=list)

    def validate(self) -> None:
        self.reason_code = _require_non_empty(self.reason_code, "reason_code")
        if not isinstance(self.tags, list):
            raise ValueError("tags must be a list")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


def _validate_email_semantic_watch_predicate(event_name: str, predicate: dict) -> None:
    """Shared validation for email_semantic_match watches. Called by both WatchRegistrationRequest and WatchRegistration."""
    if not event_name.startswith("signal_router.watch.email_"):
        raise ValueError(
            "email_semantic_match requires event_name to start with 'signal_router.watch.email_'."
        )
    semantic_query = str(predicate.get("semantic_query") or "").strip()
    if not semantic_query:
        raise ValueError("email_semantic_match requires predicate.semantic_query.")
    sender_equals = predicate.get("sender_equals")
    if sender_equals is not None and not str(sender_equals).strip():
        raise ValueError("predicate.sender_equals must be non-empty when provided.")
    sender_contains = predicate.get("sender_contains")
    if sender_contains is not None and not str(sender_contains).strip():
        raise ValueError("predicate.sender_contains must be non-empty when provided.")
    subject_contains = predicate.get("subject_contains")
    if subject_contains is not None and not str(subject_contains).strip():
        raise ValueError("predicate.subject_contains must be non-empty when provided.")
    account_id_equals = predicate.get("account_id_equals")
    if account_id_equals is not None and not str(account_id_equals).strip():
        raise ValueError("predicate.account_id_equals must be non-empty when provided.")


@dataclass
class WatchRegistrationRequest:
    """
    Registration contract consumed by watcher service.

    The watcher service is intentionally decoupled from orchestration concerns; it only
    tracks matching contracts and emits events when matches occur.
    """

    watch_key: str
    event_name: str
    watch_type: str
    predicate: Dict[str, Any]
    dedupe_window_seconds: int = 300
    expires_at_utc: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.watch_key = _require_non_empty(self.watch_key, "watch_key")
        self.event_name = _require_non_empty(self.event_name, "event_name")
        self.watch_type = _require_non_empty(self.watch_type, "watch_type")
        if not isinstance(self.predicate, dict) or not self.predicate:
            raise ValueError("predicate must be a non-empty dict")
        if self.dedupe_window_seconds < 0:
            raise ValueError("dedupe_window_seconds must be >= 0")
        if self.expires_at_utc is not None:
            self.expires_at_utc = _require_non_empty(self.expires_at_utc, "expires_at_utc")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")
        if self.watch_type == EMAIL_SEMANTIC_MATCH_WATCH_TYPE:
            _validate_email_semantic_watch_predicate(self.event_name, self.predicate)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class WatchRegistration:
    registration_id: str
    watch_key: str
    event_name: str
    watch_type: str
    predicate: Dict[str, Any]
    dedupe_window_seconds: int
    status: WatchStatus = "active"
    expires_at_utc: Optional[str] = None
    created_at_utc: str = field(default_factory=_now_utc_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_request(cls, request: WatchRegistrationRequest) -> "WatchRegistration":
        request.validate()
        return cls(
            registration_id=f"watch_{uuid4().hex}",
            watch_key=request.watch_key,
            event_name=request.event_name,
            watch_type=request.watch_type,
            predicate=dict(request.predicate),
            dedupe_window_seconds=request.dedupe_window_seconds,
            expires_at_utc=request.expires_at_utc,
            metadata=dict(request.metadata),
        )

    def validate(self) -> None:
        self.registration_id = _require_non_empty(self.registration_id, "registration_id")
        self.watch_key = _require_non_empty(self.watch_key, "watch_key")
        self.event_name = _require_non_empty(self.event_name, "event_name")
        self.watch_type = _require_non_empty(self.watch_type, "watch_type")
        if not isinstance(self.predicate, dict) or not self.predicate:
            raise ValueError("predicate must be a non-empty dict")
        if self.dedupe_window_seconds < 0:
            raise ValueError("dedupe_window_seconds must be >= 0")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")
        if self.watch_type == EMAIL_SEMANTIC_MATCH_WATCH_TYPE:
            _validate_email_semantic_watch_predicate(self.event_name, self.predicate)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class WatchMatchEvent:
    """
    Canonical emitted event payload from watcher evaluation.

    event_id should be unique per match occurrence. dedupe_key is used to prevent
    repeated emits for the same underlying source signal.
    """

    event_id: str
    event_name: str
    registration_id: str
    watch_key: str
    dedupe_key: str
    occurred_at_utc: str
    source_signal_id: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.event_id = _require_non_empty(self.event_id, "event_id")
        self.event_name = _require_non_empty(self.event_name, "event_name")
        self.registration_id = _require_non_empty(self.registration_id, "registration_id")
        self.watch_key = _require_non_empty(self.watch_key, "watch_key")
        self.dedupe_key = _require_non_empty(self.dedupe_key, "dedupe_key")
        self.occurred_at_utc = _require_non_empty(self.occurred_at_utc, "occurred_at_utc")
        self.source_signal_id = _require_non_empty(self.source_signal_id, "source_signal_id")
        if not isinstance(self.evidence, dict):
            raise ValueError("evidence must be a dict")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class WatcherAgentInput:
    watch_registration: Dict[str, Any]
    signal: Dict[str, Any]
    filter_decision: Optional[Dict[str, Any]] = None

    def validate(self) -> None:
        if not isinstance(self.watch_registration, dict) or not self.watch_registration:
            raise ValueError("watch_registration must be a non-empty dict")
        if not isinstance(self.signal, dict) or not self.signal:
            raise ValueError("signal must be a non-empty dict")
        if self.filter_decision is not None and not isinstance(self.filter_decision, dict):
            raise ValueError("filter_decision must be a dict when provided")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass
class WatcherAgentOutput:
    should_emit_event: bool
    match_reason: str
    confidence: float
    dedupe_key_hint: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    proposed_payload: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.match_reason = _require_non_empty(self.match_reason, "match_reason")
        self.dedupe_key_hint = _require_non_empty(self.dedupe_key_hint, "dedupe_key_hint")
        if not isinstance(self.confidence, (int, float)):
            raise ValueError("confidence must be numeric")
        self.confidence = float(self.confidence)
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if not isinstance(self.evidence, dict):
            raise ValueError("evidence must be a dict")
        if not isinstance(self.proposed_payload, dict):
            raise ValueError("proposed_payload must be a dict")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)
