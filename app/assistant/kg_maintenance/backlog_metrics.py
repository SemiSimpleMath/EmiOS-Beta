"""Maintenance backlog visibility (fragility review #2, move 3).

The maintenance loop's caps (investigations 20/run, executor 10/15min)
convert LLM cost into SILENT backlog — the ~1400-finding / ~100-hour
duplicate backlog accumulated with no alarm anywhere. These metrics make
queue debt observable: counts by type and status, age of the oldest open
item, and the 7-day raise-vs-drain balance. Surfaced on
/api/system/health; "raising faster than draining for a week" becomes a
morning-glance catch instead of an archaeology project.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)

# Statuses that represent open work vs finished work. "investigated" is
# open (waiting on review or the executor's grace window).
OPEN_STATUSES = ("pending", "approved", "investigated")
BACKLOG_ALARM_TOTAL = 500  # the 1400-finding incident says alarm well before that


def compute_backlog_metrics(*, window_days: int = 7) -> Dict[str, Any]:
    """Read-only snapshot of the maintenance queue."""
    from sqlalchemy import func

    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding as F
    from app.models.db_manager import get_db_manager

    now = utc_now()
    cutoff = now - timedelta(days=window_days)

    with get_db_manager().read_session() as s:
        open_by_type: Dict[str, int] = {
            f"{ftype}": count
            for ftype, count in (
                s.query(F.finding_type, func.count(F.id))
                .filter(F.status.in_(OPEN_STATUSES))
                .group_by(F.finding_type)
                .all()
            )
        }
        total_open = sum(open_by_type.values())

        oldest_open = (
            s.query(func.min(F.created_at))
            .filter(F.status.in_(OPEN_STATUSES))
            .scalar()
        )
        oldest_age_days = None
        if oldest_open is not None:
            if oldest_open.tzinfo is None:
                from app.assistant.kg_core.kg_utils.date_compare import as_aware_utc
                oldest_open = as_aware_utc(oldest_open)
            oldest_age_days = round((now - oldest_open).total_seconds() / 86400, 1)

        raised = (
            s.query(func.count(F.id)).filter(F.created_at >= cutoff).scalar() or 0
        )
        # Drained = left the open set within the window. updated_at moves on
        # the closing transition; terminal rows whose updated_at is recent
        # were closed recently (good enough for a trend signal).
        drained = (
            s.query(func.count(F.id))
            .filter(~F.status.in_(OPEN_STATUSES))
            .filter(F.updated_at >= cutoff)
            .scalar() or 0
        )

    metrics: Dict[str, Any] = {
        "total_open": total_open,
        "open_by_type": dict(sorted(open_by_type.items(), key=lambda kv: -kv[1])),
        "oldest_open_age_days": oldest_age_days,
        f"raised_{window_days}d": raised,
        f"drained_{window_days}d": drained,
        "drain_deficit": raised - drained,
    }
    return metrics
