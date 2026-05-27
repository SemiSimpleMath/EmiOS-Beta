"""Subconscious → daily_context projection.

Reads the subconscious's two canonical outputs (concerns_register from the
noticer, plan.weekly_schedule from the scheduler arbiter) and projects them
into the daily_context shape so chat_gate / strategic_planner can read both
in one place.

Architectural rule (2026-05-25): the noticer is THE channel for raw
subconscious → conscious signal. Domain proposers (meal / wellness /
romance / shopping) produce candidate intention.* pods, but those are
pipeline intermediates the arbiter picks FROM — they never leak into chat
context directly. If a meal/health/romance pattern matters, the noticer
surfaces it as a concern. The arbiter's pick lives in weekly_schedule.

Output shape:

    active_concerns_this_week:
        [{severity, horizon, title, subject, addressable_by[],
          notes (≤200 chars), evidence_count}, ...]

    active_concerns_longer_horizon:
        Same shape; horizons = this_month / this_quarter / this_year /
        longer (anything that isn't today / this_week).

    weekly_schedule_excerpt:
        {pod_id, generated_at, one_liner, body} from the most recent
        plan.weekly_schedule pod, or {} if none exists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import utc_to_local

logger = get_logger(__name__)


def _iso_utc_to_local_str(value: Any) -> str:
    """Convert an ISO-8601 UTC string to local 'YYYY-MM-DD HH:MM AM/PM'.
    Returns "" on missing / unparseable input.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        from datetime import datetime, timezone
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return utc_to_local(dt).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return ""


# Concerns with these horizons go into the "this_week" bucket; everything
# else (this_month, this_quarter, this_year, longer, unknown) goes into
# the longer_horizon bucket. High-severity concerns also get elevated
# into the this_week bucket regardless of horizon — they're load-bearing
# enough to surface immediately even if the timing is far out.
_THIS_WEEK_HORIZONS = frozenset({"today", "this_week"})
_HIGH_SEVERITIES = frozenset({"high", "critical"})

# A concern with this state has been suppressed/resolved already — never
# render it into the daily_context, regardless of severity.
_ACTIVE_REGISTER_KEY = "active"

_NOTES_MAX_CHARS = 200


def _concerns_register_path() -> Path:
    return get_repo_root() / "resources" / "subconscious" / "resource_concerns_register.json"


def _load_concerns_register() -> Dict[str, Any]:
    p = _concerns_register_path()
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("[subconscious_projection] concerns_register read failed: %s", e)
        return {}


def _project_concern(concern: Dict[str, Any]) -> Dict[str, Any]:
    """Compact projection — drops evidence details (just keeps count),
    truncates notes. Consumers that need the full concern can read
    concerns_register directly."""
    notes = str(concern.get("notes") or "")
    if len(notes) > _NOTES_MAX_CHARS:
        notes = notes[: _NOTES_MAX_CHARS - 1].rstrip() + "…"
    addressable = concern.get("addressable_by") or []
    if not isinstance(addressable, list):
        addressable = []
    return {
        "concern_id": str(concern.get("concern_id") or ""),
        "severity": str(concern.get("severity") or "unknown"),
        "horizon": str(concern.get("horizon") or "unknown"),
        "title": str(concern.get("title") or "(untitled)"),
        "subject": str(concern.get("subject") or ""),
        "addressable_by": [str(x) for x in addressable if isinstance(x, str)],
        "notes": notes,
        "evidence_count": len(concern.get("evidence") or [])
        if isinstance(concern.get("evidence"), list) else 0,
        # Freshness signal — parallel to expected_schedule items' updated_at_local.
        # Consumers use this to resolve "newer signal wins" overrides when a
        # concern contradicts an older calendar entry.
        "last_updated_local": _iso_utc_to_local_str(concern.get("last_reinforced_utc")),
    }


def _severity_sort_key(c: Dict[str, Any]) -> int:
    # Sort high→critical first, then medium, then low/unknown
    sev = (c.get("severity") or "").lower()
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(sev, 4)


def project_concerns() -> Dict[str, List[Dict[str, Any]]]:
    """Read concerns_register, split active concerns by horizon, return
    two lists ready to embed in the daily_context output.

    Returns ``{"this_week": [...], "longer_horizon": [...]}``.
    Empty lists when nothing matches (or read fails).
    """
    register = _load_concerns_register()
    active = register.get(_ACTIVE_REGISTER_KEY) or []
    if not isinstance(active, list):
        return {"this_week": [], "longer_horizon": []}

    this_week: List[Dict[str, Any]] = []
    longer: List[Dict[str, Any]] = []
    for c in active:
        if not isinstance(c, dict):
            continue
        horizon = str(c.get("horizon") or "").lower()
        severity = str(c.get("severity") or "").lower()
        projected = _project_concern(c)
        # High/critical severity elevates to this_week regardless of horizon.
        if horizon in _THIS_WEEK_HORIZONS or severity in _HIGH_SEVERITIES:
            this_week.append(projected)
        else:
            longer.append(projected)

    this_week.sort(key=_severity_sort_key)
    longer.sort(key=_severity_sort_key)
    return {"this_week": this_week, "longer_horizon": longer}


def project_weekly_schedule_excerpt() -> Dict[str, Any]:
    """Return the most recent ``plan.weekly_schedule`` pod, compactly.

    The arbiter mints one of these per daily run; consumers should see
    the latest. Empty dict when nothing exists yet (e.g. arbiter hasn't
    run since the subconscious was wired)."""
    try:
        from app.models.db_manager import get_db_manager
        from sqlalchemy import text as sql_text
    except Exception as e:
        logger.warning("[subconscious_projection] DB unavailable for weekly_schedule: %s", e)
        return {}

    try:
        with get_db_manager().read_session() as session:
            row = session.execute(
                sql_text(
                    """
                    SELECT pod_id, one_liner, body, created_at
                    FROM pod_store
                    WHERE kind = 'plan.weekly_schedule'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
    except Exception as e:
        logger.warning("[subconscious_projection] weekly_schedule read failed: %s", e)
        return {}
    if not row:
        return {}
    pod_id, one_liner, body, created_at = row
    return {
        "pod_id": str(pod_id),
        "generated_at": str(created_at or ""),
        "one_liner": str(one_liner or "").strip(),
        # Body is the markdown the arbiter wrote — include verbatim;
        # consumers render it directly in their prompts. Cap defensively
        # at 4000 chars (well above the arbiter's current ~1KB output).
        "body": (str(body or "").strip())[:4000],
    }


def project_subconscious_for_daily_context() -> Dict[str, Any]:
    """One-shot projector. Returns the three fields the daily_context
    output should gain so chat_gate + dayflow can read subconscious
    signal in one place.

    Failures in any sub-call degrade to empty; never raise.
    """
    try:
        concerns = project_concerns()
    except Exception as e:
        logger.warning("[subconscious_projection] concerns projection failed: %s", e)
        concerns = {"this_week": [], "longer_horizon": []}
    try:
        weekly = project_weekly_schedule_excerpt()
    except Exception as e:
        logger.warning("[subconscious_projection] weekly_schedule projection failed: %s", e)
        weekly = {}
    return {
        "active_concerns_this_week": concerns["this_week"],
        "active_concerns_longer_horizon": concerns["longer_horizon"],
        "weekly_schedule_excerpt": weekly,
    }
