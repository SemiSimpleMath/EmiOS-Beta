# wake_segments.py
"""
SQLAlchemy model for wake_segments table.

Wake segments track periods when the user was awake during sleep hours
(e.g., woke up at 3 AM for 30 minutes, then went back to sleep).

This allows accurate sleep calculation by subtracting awake time from sleep time.
"""
from sqlalchemy import Column, Integer, String, DateTime, Float
from app.models.base import Base


class WakeSegment(Base):
    """
    Wake segments: periods when user was awake during sleep hours.
    
    Example: User slept 11 PM - 7 AM but was awake 3 AM - 3:30 AM.
    - Sleep segment: 11 PM - 7 AM (8 hours)
    - Wake segment: 3 AM - 3:30 AM (30 minutes)
    - Net sleep: 7.5 hours
    
    Columns:
        id: Primary key
        start_time: When user woke up during the night (UTC, timezone-aware)
        end_time: When user went back to sleep (UTC, timezone-aware). NULL if not specified.
        duration_minutes: How long they were awake (calculated or estimated)
        source: How we learned about this ('user_chat', 'manual', 'activity_tracker')
        notes: Optional notes (e.g., 'bathroom', 'couldn't sleep', 'stress')
        created_at: When this record was created
    """
    __tablename__ = 'wake_segments'
    
    id = Column(Integer, primary_key=True)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=True)  # NULL if duration estimated
    duration_minutes = Column(Float, nullable=False)
    source = Column(String, nullable=False)  # 'user_chat', 'manual', 'activity_tracker'
    notes = Column(String, nullable=True)  # 'bathroom', 'couldn't sleep', etc.
    
    def __repr__(self):
        return f"<WakeSegment(id={self.id}, start='{self.start_time}', duration={self.duration_minutes}min, notes='{self.notes}')>"
