"""SQLAlchemy tables owned by the gut.

Separate from signal_router's tables so the two modules can evolve
independently. The gut owns cursor persistence for every inbound source.
"""
from __future__ import annotations

from sqlalchemy import Column, String, TIMESTAMP, func

from app.models.base import Base


class IngestCursorRow(Base):
    __tablename__ = "ingest_cursor"

    source_key = Column(String, primary_key=True)
    cursor_value = Column(String, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
