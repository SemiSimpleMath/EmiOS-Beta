"""SQLAlchemy model for the sandbox_audit table.

Every execute_code call writes one row. Tracks duration, exit code, byte
counts, pod IDs used, errors. Never logs full stdout/stderr — those could
contain secrets. Just sizes plus a 256-char stderr preview.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from app.models.base import Base


class SandboxAudit(Base):
    __tablename__ = "sandbox_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    request_id = Column(String, nullable=True, index=True)
    caller_agent = Column(String, nullable=True)
    language = Column(String(16), nullable=False)
    # sha256 hex of the source. Lets us spot duplicate runs without storing
    # the script itself (which could contain secrets).
    source_hash = Column(String(64), nullable=True, index=True)
    source_bytes = Column(Integer, nullable=True)
    duration_ms = Column(Float, nullable=True)
    exit_code = Column(Integer, nullable=True)
    timeout_s = Column(Float, nullable=True)
    # JSON-encoded lists of inputs/outputs.
    fs_allowlist = Column(Text, nullable=True)
    egress_allowlist = Column(Text, nullable=True)
    egress_logged = Column(Text, nullable=True)        # actually-attempted domains
    input_pod_ids = Column(Text, nullable=True)
    output_pod_ids = Column(Text, nullable=True)
    response_pod_id = Column(String, nullable=True)
    response_pod_kind = Column(String, nullable=True)
    # Just sizes — never log full output (secrets risk).
    stdout_bytes = Column(Integer, nullable=True)
    stderr_bytes = Column(Integer, nullable=True)
    stderr_preview = Column(Text, nullable=True)        # first 256 chars only
    error_code = Column(String, nullable=True)
    requirements = Column(Text, nullable=True)
