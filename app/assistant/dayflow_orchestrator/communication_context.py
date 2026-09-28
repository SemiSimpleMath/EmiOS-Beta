"""Read-only communication evidence for Dayflow; never infer delivery or completion."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)
HISTORY_HOURS = 18


def scheduled_reminder_history(now_utc: datetime) -> dict:
    """Full scheduled-reminder records in an explicit window, using the source/time index.

    The producer persists these BEFORE attempting delivery. They are not receipts.
    Query failures stay visibly unavailable instead of becoming an empty history.
    This function is used only in Dayflow prep, never on the ordinary chat path.
    """
    if now_utc.tzinfo is None:
        raise ValueError("scheduled reminder history requires an aware timestamp")
    now_utc = now_utc.astimezone(timezone.utc)
    since = now_utc - timedelta(hours=HISTORY_HOURS)
    history = {"status": "available", "since": since.isoformat(),
               "through": now_utc.isoformat(), "records": []}
    try:
        stmt = (select(UnifiedLog2026)
                .where(UnifiedLog2026.source == "scheduler_reminder",
                       UnifiedLog2026.room_id == "master_room",
                       UnifiedLog2026.direction == "outbound",
                       UnifiedLog2026.timestamp >= since,
                       UnifiedLog2026.timestamp <= now_utc)
                .order_by(UnifiedLog2026.timestamp, UnifiedLog2026.id))
        with get_db_manager().read_session() as session:
            history["records"] = [
                {"id": row.id, "source": row.source,
                 "timestamp": row.timestamp.isoformat(), "room_id": row.room_id,
                 "surface": row.room_surface, "text": row.message,
                 "delivery_status": "unknown", "read_status": "unknown"}
                for row in session.execute(stmt).scalars()
            ]
    except Exception:
        logger.warning("Dayflow scheduled reminder history unavailable", exc_info=True)
        history["status"] = "unavailable"
    return history
