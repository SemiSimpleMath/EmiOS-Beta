"""
Ticket Database Model
=====================

Generic ticket model for suggestions and actions across the system.
Tracks lifecycle from creation through user response and execution.
"""

from enum import Enum
from datetime import datetime, timezone
import json
from pathlib import Path
from sqlalchemy import Column, String, DateTime, Text, Integer, Index, func, JSON

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime


_TICKET_CONFIG_CACHE = None
_TICKET_CONFIG_MTIME = None


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _load_ticket_config() -> dict:
    global _TICKET_CONFIG_CACHE, _TICKET_CONFIG_MTIME
    config_path = Path(__file__).resolve().parent / "config_ticket_manager.json"
    try:
        if not config_path.exists():
            return {}
        mtime = config_path.stat().st_mtime
        if _TICKET_CONFIG_CACHE is None or _TICKET_CONFIG_MTIME != mtime:
            _TICKET_CONFIG_CACHE = json.loads(config_path.read_text(encoding="utf-8")) or {}
            _TICKET_CONFIG_MTIME = mtime
        return _TICKET_CONFIG_CACHE if isinstance(_TICKET_CONFIG_CACHE, dict) else {}
    except Exception:
        return {}


class TicketType(str, Enum):
    """Master category for a ticket — drives both DB classification and UI shape."""

    CRON_REMINDER    = "cron_reminder"
    TOOL_APPROVAL    = "tool_approval"
    DAYFLOW_ADVICE   = "dayflow_advice"
    DAYFLOW_NOTIFY   = "dayflow_notify"
    DAYFLOW_DECISION = "dayflow_decision"
    ASK_USER         = "ask_user"


class TicketState(str, Enum):
    """State machine for tickets."""

    PENDING = "pending"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class Ticket(Base):
    """
    Generic ticket model for tracking suggestions, approvals, and actions.
    """

    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(String(100), unique=True, nullable=False, index=True)

    # Type identifiers
    ticket_type = Column(String(50), nullable=False, index=True)
    suggestion_type = Column(String(50), nullable=False)

    # State tracking
    state = Column(String(50), nullable=False, default=TicketState.PENDING.value, index=True)
    state_history = Column(JSON, nullable=False, default=list)
    effects_processed = Column(Integer, default=0)
    
    # Claim tracking (for atomic cross-process claiming)
    claim_token = Column(String(36), nullable=True, index=True)
    claimed_at = Column(AwareUtcDateTime, nullable=True)

    # Content
    title = Column(String(500))
    message = Column(Text)
    action_type = Column(String(100))
    action_params = Column(JSON)
    status_effect = Column(JSON, nullable=True)

    # Trigger context
    trigger_context = Column(JSON)
    trigger_reason = Column(Text)

    # User interaction
    ask_user_id = Column(String(100))
    user_action = Column(String(50), nullable=True)
    user_text = Column(Text, nullable=True)
    user_response_parsed = Column(JSON)

    # Timestamps
    created_at = Column(AwareUtcDateTime, nullable=False, server_default=func.now(), index=True)
    proposed_at = Column(AwareUtcDateTime)
    responded_at = Column(AwareUtcDateTime)
    completed_at = Column(AwareUtcDateTime)
    updated_at = Column(AwareUtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    # Validity window
    valid_from = Column(AwareUtcDateTime)
    valid_until = Column(AwareUtcDateTime, index=True)

    # Snooze handling
    snooze_until = Column(AwareUtcDateTime, nullable=True, index=True)
    snooze_count = Column(Integer, default=0)

    # Execution tracking
    related_action_id = Column(String(100))
    execution_result = Column(Text)

    # Auto-resolution
    stale_after_minutes = Column(Integer, nullable=True)
    resolution_strategy = Column(String(50), nullable=True)
    auto_resolved_at = Column(AwareUtcDateTime, nullable=True)
    resolution_reason = Column(Text, nullable=True)
    assumed_status_effect = Column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_ticket_state_created", "state", "created_at"),
        Index("idx_ticket_type_state", "ticket_type", "state"),
        Index("idx_ticket_suggestion_state", "suggestion_type", "state"),
        Index("idx_ticket_snooze", "snooze_until"),
        Index("idx_ticket_valid_until", "valid_until"),
    )

    def __repr__(self):
        return (
            f"<Ticket(id={self.id}, ticket_id='{self.ticket_id}', "
            f"type='{self.ticket_type}', state='{self.state}')>"
        )

    def get_stale_after_minutes(self) -> int:
        """Returns stale minutes or -1 if never stale."""
        if self.stale_after_minutes is not None:
            return self.stale_after_minutes
        cfg = _load_ticket_config()
        by_ticket_type = cfg.get("stale_minutes_by_ticket_type", {}) if isinstance(cfg, dict) else {}
        by_suggestion_type = cfg.get("stale_minutes_by_suggestion_type", {}) if isinstance(cfg, dict) else {}
        if isinstance(by_ticket_type, dict) and self.ticket_type in by_ticket_type:
            return _coerce_int(by_ticket_type[self.ticket_type], 120)
        if isinstance(by_suggestion_type, dict) and self.suggestion_type in by_suggestion_type:
            return _coerce_int(by_suggestion_type[self.suggestion_type], 120)
        return _coerce_int(cfg.get("default_stale_minutes", 120), 120)

    def get_default_resolution_strategy(self) -> str:
        if self.resolution_strategy:
            return self.resolution_strategy
        cfg = _load_ticket_config()
        by_ticket_type = cfg.get("resolution_strategy_by_ticket_type", {}) if isinstance(cfg, dict) else {}
        by_suggestion_type = cfg.get("resolution_strategy_by_suggestion_type", {}) if isinstance(cfg, dict) else {}
        if isinstance(by_ticket_type, dict) and self.ticket_type in by_ticket_type:
            return by_ticket_type[self.ticket_type]
        if isinstance(by_suggestion_type, dict) and self.suggestion_type in by_suggestion_type:
            return by_suggestion_type[self.suggestion_type]
        return str(cfg.get("default_resolution_strategy", "abandoned"))

    def is_stale(self) -> bool:
        if self.state != TicketState.PROPOSED.value:
            return False
        if not self.proposed_at:
            return False

        from datetime import timedelta

        stale_minutes = self.get_stale_after_minutes()
        if stale_minutes == -1:
            return False
        stale_threshold = self.proposed_at + timedelta(minutes=stale_minutes)
        return datetime.now(timezone.utc) > stale_threshold

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "ticket_type": self.ticket_type,
            "suggestion_type": self.suggestion_type,
            "state": self.state,
            "state_history": self.state_history,
            "effects_processed": self.effects_processed,
            "claim_token": self.claim_token,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "title": self.title,
            "message": self.message,
            "action_type": self.action_type,
            "action_params": self.action_params,
            "trigger_context": self.trigger_context,
            "trigger_reason": self.trigger_reason,
            "status_effect": self.status_effect,
            "ask_user_id": self.ask_user_id,
            "user_action": self.user_action,
            "user_text": self.user_text,
            "user_response_parsed": self.user_response_parsed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "proposed_at": self.proposed_at.isoformat() if self.proposed_at else None,
            "responded_at": self.responded_at.isoformat() if self.responded_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "snooze_until": self.snooze_until.isoformat() if self.snooze_until else None,
            "snooze_count": self.snooze_count,
            "related_action_id": self.related_action_id,
            "execution_result": self.execution_result,
            "stale_after_minutes": self.stale_after_minutes,
            "resolution_strategy": self.resolution_strategy,
            "auto_resolved_at": self.auto_resolved_at.isoformat() if self.auto_resolved_at else None,
            "resolution_reason": self.resolution_reason,
            "assumed_status_effect": self.assumed_status_effect,
        }


