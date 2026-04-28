"""
SQLAlchemy models for AFK and Active tracking.
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Index, Boolean
from sqlalchemy.sql import func
from app.models.base import Base


class ActiveSegment(Base):
    """
    Records completed active sessions (when user was at the keyboard).
    """
    __tablename__ = "active_segments"

    id = Column(Integer, primary_key=True)
    start_time = Column(DateTime(timezone=True), nullable=False, index=True)
    end_time = Column(DateTime(timezone=True), nullable=False, index=True)
    duration_minutes = Column(Float, nullable=False)
    is_provisional = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_active_segment_start", "start_time"),
        Index("idx_active_segment_end", "end_time"),
    )

    def __repr__(self):
        return (
            f"<ActiveSegment(id={self.id}, start={self.start_time}, "
            f"end={self.end_time}, duration={self.duration_minutes:.1f}min)>"
        )
