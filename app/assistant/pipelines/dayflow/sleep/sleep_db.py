"""
Sleep Database Layer (pipeline-native copy).

This module provides DB helpers for sleep-related tables:
- sleep_segments (SleepSegment)
- wake_segments (WakeSegment)

The DayFlow sleep computation uses these read helpers to incorporate
user-reported sleep/wake adjustments (sources like "user_chat", "manual").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc
from sqlalchemy.exc import SQLAlchemyError

from app.assistant.utils.error_logging import log_critical_error
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager
from app.models.sleep_segments import SleepSegment
from app.models.wake_segments import WakeSegment

logger = get_logger(__name__)


def get_sleep_segments_last_24_hours() -> List[Dict[str, Any]]:
    """
    Get all sleep segments from the last 24 hours.

    Returns:
        List of sleep segment dictionaries, newest first:
        - id, start, end, duration_minutes, source, raw_mention
    """
    try:
        with get_db_manager().read_session() as session:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

            segments = (
                session.query(SleepSegment)
                .filter(SleepSegment.start_time >= cutoff_time)
                .order_by(desc(SleepSegment.start_time))
                .all()
            )

            return [
                {
                    "id": s.id,
                    "start": s.start_time.isoformat(),
                    "end": s.end_time.isoformat() if s.end_time else None,
                    "duration_minutes": s.duration_minutes,
                    "source": s.source,
                    "raw_mention": s.raw_mention,
                }
                for s in segments
            ]
    except SQLAlchemyError as e:
        log_critical_error(
            "sleep_db.get_sleep_segments_last_24_hours",
            "Failed to fetch sleep segments for last 24h",
            e,
        )
        return []


def get_wake_segments_last_24_hours() -> List[Dict[str, Any]]:
    """
    Get all wake segments from the last 24 hours.

    Returns:
        List of wake segment dictionaries, oldest first:
        - id, start_time, end_time, duration_minutes, source, notes
    """
    try:
        with get_db_manager().read_session() as session:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

            segments = (
                session.query(WakeSegment)
                .filter(WakeSegment.start_time >= cutoff_time)
                .order_by(WakeSegment.start_time)
                .all()
            )

            return [
                {
                    "id": seg.id,
                    "start_time": seg.start_time.isoformat(),
                    "end_time": seg.end_time.isoformat() if seg.end_time else None,
                    "duration_minutes": seg.duration_minutes,
                    "source": seg.source,
                    "notes": seg.notes,
                }
                for seg in segments
            ]
    except SQLAlchemyError as e:
        log_critical_error(
            "sleep_db.get_wake_segments_last_24_hours",
            "Failed to fetch wake segments for last 24h",
            e,
        )
        return []
    except Exception as e:
        # Keep a broad catch to avoid breaking DayFlow if wake table is missing.
        logger.warning("sleep_db.get_wake_segments_last_24_hours failed: %s", e)
        return []

