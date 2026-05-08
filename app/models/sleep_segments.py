"""
SQLAlchemy models for sleep tracking.
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Index
from sqlalchemy.sql import func
from app.models.base import Base
from app.assistant.utils.time_utils import AwareUtcDateTime


class SleepSegment(Base):
    """
    Records periods of sleep (continuous time blocks).
    """
    __tablename__ = "sleep_segments"

    id = Column(Integer, primary_key=True)
    start_time = Column(AwareUtcDateTime, nullable=False, index=True)
    end_time = Column(AwareUtcDateTime, nullable=True, index=True)
    duration_minutes = Column(Float, nullable=True)
    source = Column(String(50), nullable=False)
    raw_mention = Column(Text, nullable=True)
    created_at = Column(AwareUtcDateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_sleep_start", "start_time"),
        Index("idx_sleep_end", "end_time"),
        Index("idx_sleep_source", "source"),
    )

    def __repr__(self):
        return (
            f"<SleepSegment(id={self.id}, start={self.start_time}, "
            f"duration={self.duration_minutes}min, source={self.source})>"
        )
