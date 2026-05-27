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
                    id, ts_utc, agent_name, engine, duration_ms,
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
                "id": r[0],
                "ts": _fmt_ts(r[1]),
                "agent": r[2] or "(unknown)",
                "engine": r[3] or "(unknown)",
                "duration_ms": int(r[4] or 0),
                "in_tok": int(r[5] or 0),
                "out_tok": int(r[6] or 0),
                "usd": float(r[7] or 0.0),
                "status": r[8] or "",
            }
            for r in slow_rows
        ]

        # ---- failures (status != ok) --------------------------------
        fail_rows = s.execute(
            sql_text(f"""
                SELECT
                    id, ts_utc, agent_name, engine, status, duration_ms,
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
                "id": r[0],
                "ts": _fmt_ts(r[1]),
                "agent": r[2] or "(unknown)",
                "engine": r[3] or "(unknown)",
                "status": r[4] or "",
                "duration_ms": int(r[5] or 0),
                "in_tok": int(r[6] or 0),
                "out_tok": int(r[7] or 0),
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


# ---------------------------------------------------------------- detail pages


@tokens_admin_bp.route("/tokens/call/<call_id>")
def tokens_call_detail(call_id: str):
    """Single-call detail. Shows the full row + any sibling calls in
    the same caller_scope_id (i.e. other LLM calls made during the
    same manager invocation). Useful for tracing what one task cost."""
    from app.models.llm_call_log import LLMCallLog

    s = get_session()
    try:
        call = s.query(LLMCallLog).filter_by(id=call_id).one_or_none()
        if call is None:
            return render_template("tokens_call_detail.html", call=None, siblings=[]), 404

        # Pull sibling calls in the same scope (other calls in the same
        # manager invocation). Limit so a wedged loop doesn't render
        # thousands of rows.
        siblings: List[Dict] = []
        if call.caller_scope_id:
            rows = (
                s.query(LLMCallLog)
                .filter(LLMCallLog.caller_scope_id == call.caller_scope_id)
                .order_by(LLMCallLog.ts_utc.asc())
                .limit(200)
                .all()
            )
            siblings = [
                {
                    "id": r.id,
                    "ts": _fmt_ts(r.ts_utc),
                    "agent": r.agent_name,
                    "engine": r.engine,
                    "duration_ms": int(r.duration_ms or 0),
                    "in_tok": int(r.input_tokens or 0),
                    "out_tok": int(r.output_tokens or 0),
                    "usd": float(r.total_cost_usd or 0.0),
                    "status": r.status,
                    "is_current": r.id == call.id,
                }
                for r in rows
            ]

        call_view = {
            "id": call.id,
            "ts": _fmt_ts(call.ts_utc),
            "agent": call.agent_name,
            "engine": call.engine,
            "provider": call.provider,
            "in_tok": int(call.input_tokens or 0),
            "out_tok": int(call.output_tokens or 0),
            "cached_tok": int(call.cached_tokens or 0),
            "input_cost_usd": float(call.input_cost_usd or 0.0),
            "output_cost_usd": float(call.output_cost_usd or 0.0),
            "total_cost_usd": float(call.total_cost_usd or 0.0),
            "duration_ms": int(call.duration_ms or 0),
            "status": call.status,
            "caller_request_id": call.caller_request_id,
            "caller_manager_id": call.caller_manager_id,
            "caller_scope_id": call.caller_scope_id,
        }

        sibling_total_usd = sum(s_["usd"] for s_ in siblings)
        sibling_total_in = sum(s_["in_tok"] for s_ in siblings)
        sibling_total_out = sum(s_["out_tok"] for s_ in siblings)
    finally:
        s.close()

    return render_template(
        "tokens_call_detail.html",
        call=call_view,
        siblings=siblings,
        sibling_total_usd=sibling_total_usd,
        sibling_total_in=sibling_total_in,
        sibling_total_out=sibling_total_out,
    )


@tokens_admin_bp.route("/tokens/agent/<path:agent_name>")
def tokens_agent_detail(agent_name: str):
    """All calls for one agent in the selected period."""
    from app.models.llm_call_log import LLMCallLog

    period_key, period_label, cutoff, _chart_days = _resolve_period(
        request.args.get("period")
    )

    s = get_session()
    try:
        q = s.query(LLMCallLog).filter(LLMCallLog.agent_name == agent_name)
        if cutoff is not None:
            q = q.filter(LLMCallLog.ts_utc >= cutoff)

        rows = q.order_by(LLMCallLog.ts_utc.desc()).limit(200).all()

        calls = [
            {
                "id": r.id,
                "ts": _fmt_ts(r.ts_utc),
                "engine": r.engine,
                "duration_ms": int(r.duration_ms or 0),
                "in_tok": int(r.input_tokens or 0),
                "out_tok": int(r.output_tokens or 0),
                "usd": float(r.total_cost_usd or 0.0),
                "status": r.status,
                "scope_id": r.caller_scope_id,
            }
            for r in rows
        ]

        # Aggregate stats for the period (using the same filter).
        agg_q = s.query(LLMCallLog).filter(LLMCallLog.agent_name == agent_name)
        if cutoff is not None:
            agg_q = agg_q.filter(LLMCallLog.ts_utc >= cutoff)
        all_rows = agg_q.all()
        summary = {
            "calls": len(all_rows),
            "in_tok": sum(int(r.input_tokens or 0) for r in all_rows),
            "out_tok": sum(int(r.output_tokens or 0) for r in all_rows),
            "usd": sum(float(r.total_cost_usd or 0.0) for r in all_rows),
            "avg_ms": int(
                sum(int(r.duration_ms or 0) for r in all_rows) / max(1, len(all_rows))
            ),
            "max_ms": max((int(r.duration_ms or 0) for r in all_rows), default=0),
        }

        # By engine within this agent
        engine_totals: Dict[str, Dict] = {}
        for r in all_rows:
            e = r.engine or "(unknown)"
            d = engine_totals.setdefault(e, {"calls": 0, "in_tok": 0, "out_tok": 0, "usd": 0.0})
            d["calls"] += 1
            d["in_tok"] += int(r.input_tokens or 0)
            d["out_tok"] += int(r.output_tokens or 0)
            d["usd"] += float(r.total_cost_usd or 0.0)
        engines = sorted(
            ({"engine": k, **v} for k, v in engine_totals.items()),
            key=lambda x: -x["usd"],
        )
    finally:
        s.close()

    return render_template(
        "tokens_agent_detail.html",
        agent_name=agent_name,
        period_key=period_key,
        period_label=period_label,
        periods=list(_PERIODS.keys()),
        summary=summary,
        calls=calls,
        engines=engines,
        showing=min(len(rows) if rows else 0, 200),
        total_calls=summary["calls"],
    )
