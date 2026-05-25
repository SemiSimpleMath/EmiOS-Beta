"""Subconscious → daily_context projection.

Reads the subconscious's outputs (concerns_register, intention.* pods,
plan.weekly_schedule) and produces compact, horizon-stratified fields
that downstream consumers (chat_gate, dayflow situation_audit, etc.)
read directly from the daily_context output JSON.

Why this lives here and not in the subconscious module:

- The subconscious WRITES authoritative state (concerns_register +
  pod_store). This file READS that state and projects today-relevant
  slices into the daily_context shape so consumers don't each have to
  re-read concerns_register + scan pod_store independently.
- Single point of fan-out: one projection serves both chat_gate and
  dayflow. New consumers automatically inherit.
- Read-only: this module never mutates concerns_register, pods, or
  beliefs. Failures here degrade the daily_context (empty fields) but
  don't corrupt subconscious state.

Horizon stratification handles the day-vs-multi-week tension. A 2-week
out birthday concern stays VISIBLE under ``longer_horizon`` instead of
being squeezed out by today's narrative synthesizer.

Output shape (each function returns a JSON-serializable list/dict):

    active_concerns_this_week:
        [{severity, horizon, title, subject, addressable_by[],
          notes (≤200 chars), evidence_count}, ...]

    active_concerns_longer_horizon:
        Same shape; horizons = this_month / this_quarter / this_year /
        longer (anything that isn't today / this_week).

    upcoming_intentions_2w:
        [{date, kind, one_liner, pod_id}, ...] — intention.* pods
        minted in the last 14 days, sorted by date ascending.

    weekly_schedule_excerpt:
        {pod_id, generated_at, one_liner, items[]} from the most recent
        plan.weekly_schedule pod, or {} if none exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


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
_INTENTION_LOOKBACK_DAYS = 14


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


def project_upcoming_intentions(*, lookback_days: int = _INTENTION_LOOKBACK_DAYS) -> List[Dict[str, Any]]:
    """Read recent intention.* pods (excluding the _set rollup containers)
    from pod_store and project them as a compact list.

    The _set pods are summary containers minted by each proposer per run;
    they aggregate the day's proposals into one record. Daily_context
    consumers want the individual intentions (each intention.meal,
    intention.wellness, intention.romantic) — not the rollups.
    """
    try:
        from app.models.db_manager import get_db_manager
        from sqlalchemy import text as sql_text
    except Exception as e:
        logger.warning("[subconscious_projection] DB unavailable: %s", e)
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    try:
        with get_db_manager().read_session() as session:
            rows = session.execute(
                sql_text(
                    """
                    SELECT pod_id, kind, one_liner, created_at, metadata_json
                    FROM pod_store
                    WHERE kind LIKE 'intention.%'
                      AND kind NOT LIKE '%_set'
                      AND created_at >= :cutoff
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                ),
                {"cutoff": cutoff},
            ).fetchall()
    except Exception as e:
        logger.warning("[subconscious_projection] intention pods read failed: %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        pod_id, kind, one_liner, created_at, metadata_json = r
        # Try to extract a target date from metadata. Intention pods
        # generally carry their scheduled date in metadata.date or
        # metadata.scheduled_at; if neither, fall back to created_at.
        target_date = ""
        try:
            md = json.loads(metadata_json) if metadata_json else {}
            if isinstance(md, dict):
                for key in ("date", "scheduled_at", "scheduled_for", "target_date"):
                    val = md.get(key)
                    if isinstance(val, str) and val.strip():
                        target_date = val.strip()
                        break
        except Exception:
            md = {}
        if not target_date:
            target_date = (created_at or "")[:10]
        out.append({
            "pod_id": str(pod_id),
            "kind": str(kind),
            "one_liner": str(one_liner or "").strip()[:160],
            "date": target_date,
        })
    # Sort by date ascending so "next up" lands first
    out.sort(key=lambda x: x["date"])
    return out


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
    """One-shot projector. Returns the four fields the daily_context
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
        intentions = project_upcoming_intentions()
    except Exception as e:
        logger.warning("[subconscious_projection] intentions projection failed: %s", e)
        intentions = []
    try:
        weekly = project_weekly_schedule_excerpt()
    except Exception as e:
        logger.warning("[subconscious_projection] weekly_schedule projection failed: %s", e)
        weekly = {}
    return {
        "active_concerns_this_week": concerns["this_week"],
        "active_concerns_longer_horizon": concerns["longer_horizon"],
        "upcoming_intentions_2w": intentions,
        "weekly_schedule_excerpt": weekly,
    }
