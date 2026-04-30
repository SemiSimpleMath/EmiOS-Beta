# afk_db.py
"""
AFK Database Layer

Handles all database operations for active/AFK tracking:
- Recording active sessions (when user is at keyboard)
- Querying active segments
- Cleanup of old records

The model is "Active-First":
- We record when user IS active (positive evidence)
- AFK time = gaps between active segments
- No data = unknown (conservative default)
"""
from datetime import datetime, timedelta, timezone
import time
from typing import List, Optional, Dict, Any
from sqlalchemy import desc, asc
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import OperationalError

from app.models.base import get_session
from app.models.active_segments import ActiveSegment
from app.models.base import Base
from app.assistant.utils.error_logging import log_critical_error
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)



_table_initialized = False


def _is_sqlite_locked_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return "database is locked" in msg or "database table is locked" in msg or "sqlite busy" in msg


def _commit_with_retry(
    session,
    *,
    op: str,
    max_attempts: int = 6,
    base_delay_s: float = 0.05,
) -> None:
    """
    SQLite can throw transient `database is locked` when multiple writers collide.
    Even with WAL + busy_timeout, we can still hit contention in practice (esp. on Windows).

    This helper retries the commit with exponential backoff.
    """
    attempts = max(1, int(max_attempts))
    for i in range(attempts):
        try:
            session.commit()
            return
        except OperationalError as e:
            try:
                session.rollback()
            except Exception:
                pass
            if _is_sqlite_locked_error(e) and i < attempts - 1:
                delay = min(1.0, float(base_delay_s) * (2**i))
                logger.warning("SQLite locked during %s; retrying in %.2fs (%d/%d)", op, delay, i + 1, attempts)
                time.sleep(delay)
                continue
            raise


def _init_table(session) -> None:
    """Create active_segments table if it doesn't exist."""
    global _table_initialized
    if _table_initialized:
        return
    try:
        Base.metadata.create_all(session.bind, tables=[
            ActiveSegment.__table__
        ], checkfirst=True)
        _table_initialized = True
    except Exception as e:
        logger.warning(f"Failed to create active_segments table: {e}")


# =============================================================================
# Active Segment Functions
# =============================================================================

def create_provisional_segment(start_time_utc: datetime) -> Optional[int]:
    """
    Create a provisional (open) active segment when user becomes active.
    
    The segment is created with end_time = start_time, duration = 0, and is_provisional = True.
    It should be updated periodically and finalized when user goes AFK.
    
    Args:
        start_time_utc: When user became active (UTC)
    
    Returns:
        Segment ID if successful, None otherwise
    """
    session = get_session()
    try:
        _init_table(session)
        
        active_segment = ActiveSegment(
            start_time=start_time_utc,
            end_time=start_time_utc,  # Provisional: end = start
            duration_minutes=0.0,
            is_provisional=True,  # Mark as provisional (open)
        )
        session.add(active_segment)
        _commit_with_retry(session, op="afk_db.create_provisional_segment")
        
        segment_id = active_segment.id
        logger.info(f"Created provisional segment ID={segment_id}")
        return segment_id

    except SQLAlchemyError as e:
        session.rollback()
        log_critical_error(
            message="Failed to create provisional segment",
            exception=e,
            context="afk_db.create_provisional_segment",
        )
        return None
    finally:
        session.close()


def update_segment(segment_id: int, end_time_utc: datetime, finalize: bool = False) -> bool:
    """
    Update an existing segment's end_time and duration.
    
    Used to periodically update provisional segments and finalize them.
    
    Args:
        segment_id: ID of segment to update
        end_time_utc: New end time (UTC)
        finalize: If True, set is_provisional=False (segment is complete)
    
    Returns:
        True if successful, False otherwise
    """
    session = get_session()
    try:
        _init_table(session)
        
        segment = session.query(ActiveSegment).filter(
            ActiveSegment.id == segment_id
        ).first()
        
        if not segment:
            logger.warning(f"Segment ID={segment_id} not found for update")
            return False
        
        # Calculate new duration
        start_time = segment.start_time
        if hasattr(start_time, 'tzinfo') and start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        
        duration_minutes = (end_time_utc - start_time).total_seconds() / 60.0
        if duration_minutes < 0:
            duration_minutes = 0.0
        
        segment.end_time = end_time_utc
        segment.duration_minutes = duration_minutes
        
        if finalize:
            segment.is_provisional = False
        
        _commit_with_retry(session, op="afk_db.update_segment")
        
        return True

    except SQLAlchemyError as e:
        session.rollback()
        log_critical_error(
            message=f"Failed to update segment ID={segment_id}",
            exception=e,
            context="afk_db.update_segment",
        )
        return False
    finally:
        session.close()


def get_open_segment(max_age_minutes: int = 30) -> Optional[Dict[str, Any]]:
    """
    Find an open (provisional) segment from a previous session.
    
    An "open" segment is one where:
    - is_provisional = True
    - end_time is within the last max_age_minutes (safety check for stale segments)
    
    Args:
        max_age_minutes: How recent the end_time must be to consider recovering
    
    Returns:
        Segment dict if found, None otherwise
    """
    session = get_session()
    try:
        _init_table(session)
        
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        
        # Look for provisional segment that's recent enough
        segment = session.query(ActiveSegment).filter(
            ActiveSegment.is_provisional == True
        ).order_by(desc(ActiveSegment.end_time)).first()
        
        if not segment:
            return None
        
        # Check if end_time is recent enough to be worth recovering
        end_time = segment.end_time
        if hasattr(end_time, 'tzinfo') and end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        
        if end_time >= cutoff:
            return {
                'id': segment.id,
                'start_time': segment.start_time,
                'end_time': segment.end_time,
                'duration_minutes': segment.duration_minutes,
                'is_provisional': segment.is_provisional,
            }
        else:
            # Segment is too old - finalize it at its last known time
            segment.is_provisional = False
            _commit_with_retry(session, op="afk_db.get_open_segment(finalize_stale)")
            logger.info(f"Finalized stale provisional segment ID={segment.id}")
        
        return None

    except SQLAlchemyError as e:
        log_critical_error(
            message="Failed to check for open segment",
            exception=e,
            context="afk_db.get_open_segment",
        )
        return None
    finally:
        session.close()


def get_active_segments_since(since_utc: datetime) -> List[ActiveSegment]:
    """
    Get FINALIZED active segments that overlap with or are after since_utc.
    
    Excludes provisional (open) segments to avoid double-counting with
    the current active session tracked in memory.
    
    Returns raw ORM objects for statistics computation.
    """
    session = get_session()
    try:
        _init_table(session)
        
        segments = session.query(ActiveSegment).filter(
            ActiveSegment.end_time >= since_utc,
            ActiveSegment.is_provisional == False  # Exclude provisional segments
        ).order_by(asc(ActiveSegment.start_time)).all()
        
        # Detach from session
        for seg in segments:
            session.expunge(seg)
        
        return segments

    except SQLAlchemyError as e:
        log_critical_error(
            message=f"Failed to fetch active segments since {since_utc}",
            exception=e,
            context="afk_db.get_active_segments_since",
        )
        return []
    finally:
        session.close()


def get_active_segments_overlapping_range(
    start_utc: datetime,
    end_utc: datetime,
    include_provisional: bool = True,
) -> List[ActiveSegment]:
    """
    Get active segments that overlap a time range.

    Args:
        start_utc: Range start (UTC)
        end_utc: Range end (UTC)
        include_provisional: Include open segments if True

    Returns:
        List of ActiveSegment ORM objects (detached)
    """
    session = get_session()
    try:
        _init_table(session)

        query = session.query(ActiveSegment).filter(
            ActiveSegment.start_time <= end_utc,
            ActiveSegment.end_time >= start_utc,
        )
        if not include_provisional:
            query = query.filter(ActiveSegment.is_provisional == False)

        segments = query.order_by(asc(ActiveSegment.start_time)).all()

        for seg in segments:
            session.expunge(seg)

        return segments
    except SQLAlchemyError as e:
        log_critical_error(
            message=f"Failed to fetch active segments between {start_utc} and {end_utc}",
            exception=e,
            context="afk_db.get_active_segments_overlapping_range",
        )
        return []
    finally:
        session.close()


def get_recent_active_segments(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Get active segments from the last N hours.
    
    Returns:
        List of active segment dictionaries, oldest first
    """
    session = get_session()
    try:
        _init_table(session)
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        segments = session.query(ActiveSegment).filter(
            ActiveSegment.end_time >= cutoff_time
        ).order_by(asc(ActiveSegment.start_time)).all()

        result = [
            {
                'id': s.id,
                'start_time': s.start_time.isoformat(),
                'end_time': s.end_time.isoformat(),
                'duration_minutes': s.duration_minutes,
                'created_at': s.created_at.isoformat() if s.created_at else None,
            }
            for s in segments
        ]
        return result

    except SQLAlchemyError as e:
        log_critical_error(
            message=f"Failed to fetch active segments for last {hours}h",
            exception=e,
            context="afk_db.get_recent_active_segments",
        )
        return []
    finally:
        session.close()


def get_last_active_segment() -> Optional[Dict[str, Any]]:
    """
    Get the most recent active segment.
    """
    session = get_session()
    try:
        _init_table(session)
        segment = session.query(ActiveSegment).order_by(desc(ActiveSegment.end_time)).first()

        if segment:
            return {
                'id': segment.id,
                'start_time': segment.start_time.isoformat(),
                'end_time': segment.end_time.isoformat(),
                'duration_minutes': segment.duration_minutes,
            }
        return None

    except SQLAlchemyError as e:
        log_critical_error(
            message="Failed to fetch last active segment",
            exception=e,
            context="afk_db.get_last_active_segment",
        )
        return None
    finally:
        session.close()


def cleanup_old_active_segments(days: int = 7) -> int:
    """
    Delete active segments older than N days.
    
    Returns:
        Number of records deleted
    """
    session = get_session()
    try:
        _init_table(session)
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)

        deleted_count = session.query(ActiveSegment).filter(
            ActiveSegment.end_time < cutoff_time
        ).delete()

        _commit_with_retry(session, op="afk_db.cleanup_old_active_segments")
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old active segments (>{days} days)")
        
        return deleted_count

    except SQLAlchemyError as e:
        session.rollback()
        log_critical_error(
            message=f"Failed to cleanup active segments older than {days} days",
            exception=e,
            context="afk_db.cleanup_old_active_segments",
        )
        return 0
    finally:
        session.close()
