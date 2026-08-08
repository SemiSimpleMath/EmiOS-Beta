"""SQLAlchemy model for system audit cases — the register of the system's own
failures (System Auditor, 2026-08-08).

One row per case. A case is opened by user friction (the audit signal
classifier heard "this is wrong / why again / makes no sense" about the assistant's own
behavior) or by a situation_auditor finding. It is ID-BOUND: bound_ids carries
the message/ticket/work/room ids the case hangs on — dedup and recurrence are
id joins, never wording similarity.

Lifecycle (enforced by system_audit.case_store, not here):
  open -> assembled -> investigated -> awaiting_claude -> resolved | dismissed
  regressed: terminal-adjacent flag state — a NEW case whose implicated
  subsystem matches an already-resolved case (the fix didn't hold).

Repairs are NEVER executed by the system itself: the pipeline ends at a dossier in the
Claude inbox; resolutions flow back from interactive Claude Code sessions.
"""
from sqlalchemy import Column, DateTime, Float, String, Text, JSON

from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now


class SystemAuditCase(Base):
    __tablename__ = "system_audit_case"

    id = Column(String, primary_key=True)                 # "sac_" + uuid4 hex[:12]
    opened_at = Column(AwareUtcDateTime, default=utc_now, nullable=False)
    updated_at = Column(AwareUtcDateTime, default=utc_now, nullable=False)

    trigger_kind = Column(String, nullable=False)          # user_friction | auditor_finding
    status = Column(String, nullable=False, default="open", index=True)

    room_id = Column(String, nullable=True, index=True)
    # {"message_ids": [...], "ticket_ids": [...], "work_ids": [...], "agents": [...]}
    bound_ids = Column(JSON, nullable=False, default=dict)
    # [{"quote": ..., "message_id": ..., "at": iso, "kind": ...}, ...]
    friction_quotes = Column(JSON, nullable=False, default=list)

    summary = Column(Text, nullable=True)                  # one-line what-happened
    anchor_at = Column(AwareUtcDateTime, nullable=False)   # the moment evidence windows center on

    dossier_path = Column(String, nullable=True)           # data/claude_audit_inbox/case_<id>.md
    preliminary_read = Column(Text, nullable=True)         # investigator's causal chain
    implicated_subsystem = Column(String, nullable=True, index=True)
    # [{"level": "prompt"|"config"|"code", "description": ...}, ...]
    repair_suggestions = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)

    # {"commits": [...], "disposition": ..., "notes": ..., "resolved_at": iso}
    resolution = Column(JSON, nullable=True)
    recurrence_of = Column(String, nullable=True)          # resolved case id this regresses
