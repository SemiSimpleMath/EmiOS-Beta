"""SQLAlchemy model for the http_audit table.

Every http_request call writes one row. Tracks method, host, status,
response bytes, pod_ids used, agent caller, and error if any. Audit is
best-effort logging — a failure to write should not block the HTTP call
itself.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from app.models.base import Base


class HttpAudit(Base):
    __tablename__ = "http_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    request_id = Column(String, nullable=True, index=True)
    caller_agent = Column(String, nullable=True)
    method = Column(String(8), nullable=False)
    url_host = Column(String, nullable=False)
    url_path = Column(String, nullable=False)
    status_code = Column(Integer, nullable=True)
    request_bytes = Column(Integer, nullable=True)
    response_bytes = Column(Integer, nullable=True)
    # JSON-encoded list of pod_ids that were resolved during this call
    # (auth headers, body pods, response pod if sealed).
    pod_ids_used = Column(Text, nullable=True)
    response_pod_id = Column(String, nullable=True)
    response_pod_kind = Column(String, nullable=True)
    error_code = Column(String, nullable=True)
    duration_ms = Column(Float, nullable=True)
