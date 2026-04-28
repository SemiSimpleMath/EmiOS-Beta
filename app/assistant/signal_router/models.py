from __future__ import annotations

from sqlalchemy import Column, Integer, JSON, String, TIMESTAMP, func

from app.models.base import Base


class SignalRouterWatchRow(Base):
    __tablename__ = "signal_router_watch"

    registration_id = Column(String, primary_key=True)
    watch_key = Column(String, nullable=False, index=True)
    event_name = Column(String, nullable=False, index=True)
    watch_type = Column(String, nullable=False)
    predicate_json = Column(JSON, nullable=False)
    dedupe_window_seconds = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="active")
    expires_at_utc = Column(String, nullable=True)
    created_at_utc = Column(String, nullable=False)
    metadata_json = Column(JSON, nullable=False, default=dict)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class SignalRouterDedupeRow(Base):
    __tablename__ = "signal_router_dedupe"

    dedupe_key = Column(String, primary_key=True)
    registration_id = Column(String, nullable=False, index=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now(), index=True)


class SignalRouterCursorRow(Base):
    __tablename__ = "signal_router_cursor"

    source_key = Column(String, primary_key=True)
    cursor_value = Column(String, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
