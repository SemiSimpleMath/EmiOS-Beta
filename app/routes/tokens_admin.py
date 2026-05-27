"""/tokens admin page — rollups of llm_call_log.

Five panels, all driven by the same `period` query param
(?period=today|24h|7d|30d|all, default today):

  1. Summary tiles (total $, total calls, total tokens, avg duration)
  2. Daily cost bars (last N days; N depends on period)
  3. Per-agent rollup (top spenders)
  4. Per-engine rollup
  5. Slowest calls (catches runaways)
  6. Recent failures (status != ok)

All queries hit the same `llm_call_log` table. No joins. Designed to
work at scale up to a few hundred thousand rows on SQLite without an
optimizer hint — the (agent, ts) and (engine, ts) indexes already
exist on the table.

Phase 3 of the token-tracking program. Phase 4 (anomaly check) and 5
(cost guards) build on these same rollup queries.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, render_template, request
from sqlalchemy import text as sql_text

from app.models.base import get_session


def _fmt_ts(value: Any) -> str:
    """Render a timestamp from a SQLAlchemy text() row.

    SQLite returns AwareUtcDateTime columns as ISO strings, not datetimes.
    Normalize both forms to a compact `YYYY-MM-DD HH:MM:SS` (UTC) string.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    s = str(value)
    # SQLite emits ISO format; trim sub-second precision and TZ if present.
    if "T" in s:
        s = s.replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    if "+" in s:
        s = s.split("+")[0]
    return s.strip()


tokens_admin_bp = Blueprint("tokens_admin", __name__)


_PERIODS = {
    "today":  ("Today",       0,   1),    # since midnight UTC
    "24h":    ("Last 24h",   24,   1),
    "7d":     ("Last 7d",  7*24,   7),
    "30d":    ("Last 30d", 30*24, 14),    # 14-day chart even when period is 30d
    "all":    ("All time",  None, 14),
}


def _resolve_period(name: Optional[str]) -> Tuple[str, str, Optional[datetime], int]:
    """Returns (key, label, cutoff_utc_or_none, chart_days)."""
    key = (name or "today").strip().lower()
    if key not in _PERIODS:
        key = "today"
    label, hours, chart_days = _PERIODS[key]
    cutoff: Optional[datetime] = None
    now_utc = datetime.now(timezone.utc)
    if key == "today":
        cutoff = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    elif hours is not None:
        cutoff = now_utc - timedelta(hours=hours)
    # else: all time → no cutoff
    return key, label, cutoff, chart_days


def _cutoff_clause(cutoff: Optional[datetime]) -> Tuple[str, Dict]:
    if cutoff is None:
        return "", {}
    return "WHERE ts_utc >= :cutoff", {"cutoff": cutoff}


@tokens_admin_bp.route("/tokens")
def tokens_admin():
    period_key, period_label, cutoff, chart_days = _resolve_period(
        request.args.get("period")
    )
    where, params = _cutoff_clause(cutoff)

    s = get_session()
    try:
        # ---- summary ------------------------------------------------
        row = s.execute(
            sql_text(f"""
                SELECT
                    COUNT(*)             AS calls,
                    COALESCE(SUM(input_tokens), 0)   AS in_tok,
                    COALESCE(SUM(output_tokens), 0)  AS out_tok,
                    COALESCE(SUM(total_cost_usd), 0) AS usd,
                    COALESCE(AVG(duration_ms), 0)    AS avg_ms
                FROM llm_call_log
                {where}
            """),
            params,
        ).fetchone()
        summary = {
            "calls":   int(row[0] or 0),
            "in_tok":  int(row[1] or 0),
            "out_tok": int(row[2] or 0),
            "usd":     float(row[3] or 0.0),
            "avg_ms":  int(row[4] or 0),
        }

        # ---- daily cost bars (last N days; ignores `period` to keep
        # the chart consistent — see comment on _PERIODS) -------------
        days_back = chart_days
        daily_cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        daily_rows = s.execute(
            sql_text("""
                SELECT
                    date(ts_utc, 'localtime')                  AS day,
                    COALESCE(SUM(total_cost_usd), 0)           AS usd,
                    COUNT(*)                                   AS calls
                FROM llm_call_log
                WHERE ts_utc >= :cutoff
                GROUP BY day
                ORDER BY day
            """),
            {"cutoff": daily_cutoff},
        ).fetchall()
        daily = [
            {"day": r[0], "usd": float(r[1] or 0.0), "calls": int(r[2] or 0)}
            for r in daily_rows
        ]
        daily_max = max((d["usd"] for d in daily), default=0.0)

        # ---- per-agent rollup ---------------------------------------
        agent_rows = s.execute(
            sql_text(f"""
                SELECT
                    agent_name,
                    COUNT(*) AS calls,
                    COALESCE(SUM(input_tokens), 0)   AS in_tok,
                    COALESCE(SUM(output_tokens), 0)  AS out_tok,
                    COALESCE(SUM(cached_tokens), 0)  AS cached_tok,
                    COALESCE(SUM(total_cost_usd), 0) AS usd,
                    COALESCE(AVG(duration_ms), 0)    AS avg_ms,
                    COALESCE(MAX(duration_ms), 0)    AS max_ms
                FROM llm_call_log
                {where}
                GROUP BY agent_name
                ORDER BY usd DESC
                LIMIT 50
            """),
            params,
        ).fetchall()
        agents = [
            {
                "agent_name": r[0] or "(unknown)",
                "calls": int(r[1] or 0),
                "in_tok": int(r[2] or 0),
                "out_tok": int(r[3] or 0),
                "cached_tok": int(r[4] or 0),
                "usd": float(r[5] or 0.0),
                "avg_ms": int(r[6] or 0),
                "max_ms": int(r[7] or 0),
            }
            for r in agent_rows
        ]

        # ---- per-engine rollup --------------------------------------
        engine_rows = s.execute(
            sql_text(f"""
                SELECT
                    engine,
                    provider,
                    COUNT(*) AS calls,
                    COALESCE(SUM(input_tokens), 0)   AS in_tok,
                    COALESCE(SUM(output_tokens), 0)  AS out_tok,
                    COALESCE(SUM(total_cost_usd), 0) AS usd
                FROM llm_call_log
                {where}
                GROUP BY engine, provider
                ORDER BY usd DESC
                LIMIT 50
            """),
            params,
        ).fetchall()
        engines = [
            {
                "engine": r[0] or "(unknown)",
                "provider": r[1] or "(unknown)",
                "calls": int(r[2] or 0),
                "in_tok": int(r[3] or 0),
                "out_tok": int(r[4] or 0),
                "usd": float(r[5] or 0.0),
            }
            for r in engine_rows
        ]

        # ---- slowest calls (catches runaways) -----------------------
        slow_rows = s.execute(
            sql_text(f"""
                SELECT
                    ts_utc, agent_name, engine, duration_ms,
                    input_tokens, output_tokens, total_cost_usd, status
                FROM llm_call_log
                {where}
                ORDER BY duration_ms DESC
                LIMIT 20
            """),
            params,
        ).fetchall()
        slowest = [
            {
                "ts": _fmt_ts(r[0]),
                "agent": r[1] or "(unknown)",
                "engine": r[2] or "(unknown)",
                "duration_ms": int(r[3] or 0),
                "in_tok": int(r[4] or 0),
                "out_tok": int(r[5] or 0),
                "usd": float(r[6] or 0.0),
                "status": r[7] or "",
            }
            for r in slow_rows
        ]

        # ---- failures (status != ok) --------------------------------
        fail_rows = s.execute(
            sql_text(f"""
                SELECT
                    ts_utc, agent_name, engine, status, duration_ms,
                    input_tokens, output_tokens
                FROM llm_call_log
                {where}{' AND' if where else 'WHERE'} status != 'ok'
                ORDER BY ts_utc DESC
                LIMIT 25
            """),
            params,
        ).fetchall()
        failures = [
            {
                "ts": _fmt_ts(r[0]),
                "agent": r[1] or "(unknown)",
                "engine": r[2] or "(unknown)",
                "status": r[3] or "",
                "duration_ms": int(r[4] or 0),
                "in_tok": int(r[5] or 0),
                "out_tok": int(r[6] or 0),
            }
            for r in fail_rows
        ]

        # ---- status distribution ------------------------------------
        status_rows = s.execute(
            sql_text(f"""
                SELECT status, COUNT(*) AS n
                FROM llm_call_log
                {where}
                GROUP BY status
                ORDER BY n DESC
            """),
            params,
        ).fetchall()
        statuses = [{"status": r[0] or "", "count": int(r[1] or 0)} for r in status_rows]

    finally:
        s.close()

    return render_template(
        "tokens_admin.html",
        period_key=period_key,
        period_label=period_label,
        periods=list(_PERIODS.keys()),
        summary=summary,
        daily=daily,
        daily_max=daily_max,
        chart_days=chart_days,
        agents=agents,
        engines=engines,
        slowest=slowest,
        failures=failures,
        statuses=statuses,
    )
