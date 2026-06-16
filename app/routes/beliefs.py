"""Owner-only /beliefs management page + API — browse and correct the v2 belief set.

Local-only surface (``reject_if_not_local``) over the belief-engine v2 store
(belief_v2_live.db). Lists every belief with domain / kind / status / strength
and lets the owner CORRECT it: reclassify domain, suppress (remove), lock
against automated revision, or override the statement text.

Edits are DURABLE OVERRIDES, never row edits — the v2 `beliefs` table is a
projection rebuilt from the observation log, so a direct edit would be wiped by
the next rebuild_projection(). Instead:
  - domain                               -> belief_category        (side table)
  - suppress / lock / statement_override -> belief_user_override   (side table)
Both survive projection rebuilds; the retrieval read path respects suppress +
statement_override and carries the lock, so a correction here reaches the meal
planner (and future consumers) and the pipeline can't silently re-derive over it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.routes._security import reject_if_not_local
from belief_engine_v2 import tags as belief_tags_mod

logger = get_logger(__name__)

beliefs_admin_bp = Blueprint("beliefs_admin", __name__)
# Owner-only: full belief set with evidence + edit controls; never a proxied request.
beliefs_admin_bp.before_request(reject_if_not_local)

# Canonical domain vocabulary (configs/belief_domains.yaml ids).
_DOMAINS = ["routine", "health", "food", "meal", "communication", "sleep", "work", "general"]

# Trends (the dashboard's second tab) read the v2 observation log directly. Only real
# user-driven signal counts toward movement — the engine's own bookkeeping (seed replay,
# canonicalization, deprecation) is excluded so the counts reflect what the user actually did.
_REAL_SOURCES = ("daily_insight", "daily_insights", "ticket_acceptance", "ticket_rejection", "user_comment")
_TREND_WINDOW_DAYS = 21
_TREND_MIN_NET = 2
_TREND_LIMIT = 15


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _db_path() -> Path:
    return Path(get_repo_root()) / "belief_engine_v2" / "data" / "belief_v2_live.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Override side tables (durable; respected by the read path). Created lazily
    # so the page works on a store that predates them.
    conn.execute("CREATE TABLE IF NOT EXISTS belief_category (belief_id TEXT PRIMARY KEY, domain TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_user_override ("
        " belief_id TEXT PRIMARY KEY,"
        " suppressed INTEGER NOT NULL DEFAULT 0,"
        " locked INTEGER NOT NULL DEFAULT 0,"
        " statement_override TEXT,"
        " updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_short_id ("
        " belief_id TEXT PRIMARY KEY,"
        " short_id INTEGER NOT NULL UNIQUE,"
        " assigned_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_tags ("
        " belief_id TEXT NOT NULL,"
        " tag TEXT NOT NULL,"
        " assigned_at TEXT,"
        " method TEXT,"
        " PRIMARY KEY (belief_id, tag))"
    )
    return conn


@beliefs_admin_bp.route("/beliefs")
def beliefs_page():
    return render_template("beliefs.html", domains=_DOMAINS, tags_vocab=list(belief_tags_mod.vocab().keys()))


@beliefs_admin_bp.route("/api/beliefs/list")
def beliefs_list():
    """All beliefs (filterable) with their merged override state. Filtering is
    server-side so big result sets stay light; the full evidence trail is fetched
    per-belief via /item."""
    domain = (request.args.get("domain") or "").strip()
    kind = (request.args.get("kind") or "").strip()
    status = (request.args.get("status") or "active").strip()
    q = (request.args.get("q") or "").strip().lower()
    include_suppressed = request.args.get("include_suppressed") == "1"

    sql = [
        "SELECT b.belief_id,",
        " COALESCE(o.statement_override, b.statement_nl) AS statement,",
        " (o.statement_override IS NOT NULL) AS edited,",
        " b.kind, b.status, b.support, b.contradiction, b.net, b.obs_count, b.last_observed,",
        " c.domain, COALESCE(o.suppressed,0) AS suppressed, COALESCE(o.locked,0) AS locked,",
        " ('b' || s.short_id) AS short_id,",
        " (SELECT GROUP_CONCAT(tag) FROM belief_tags WHERE belief_id=b.belief_id) AS tags",
        " FROM beliefs b",
        " LEFT JOIN belief_category c ON c.belief_id = b.belief_id",
        " LEFT JOIN belief_user_override o ON o.belief_id = b.belief_id",
        " LEFT JOIN belief_short_id s ON s.belief_id = b.belief_id",
        " WHERE 1=1",
    ]
    params: list = []
    if status and status != "all":
        sql.append(" AND b.status=?"); params.append(status)
    if domain:
        sql.append(" AND c.domain=?"); params.append(domain)
    if kind:
        sql.append(" AND b.kind=?"); params.append(kind)
    if q:
        sql.append(" AND lower(COALESCE(o.statement_override, b.statement_nl)) LIKE ?"); params.append(f"%{q}%")
    if not include_suppressed:
        sql.append(" AND COALESCE(o.suppressed,0)=0")
    sql.append(" ORDER BY b.obs_count DESC, b.belief_id")

    conn = _connect()
    try:
        rows = conn.execute("".join(sql), params).fetchall()
        kinds = [r[0] for r in conn.execute(
            "SELECT DISTINCT kind FROM beliefs WHERE kind IS NOT NULL ORDER BY 1")]
        dcounts = {(r[0] or "(none)"): r[1] for r in conn.execute(
            "SELECT c.domain, COUNT(*) FROM beliefs b "
            "LEFT JOIN belief_category c ON c.belief_id=b.belief_id "
            "LEFT JOIN belief_user_override o ON o.belief_id=b.belief_id "
            "WHERE b.status='active' AND COALESCE(o.suppressed,0)=0 GROUP BY c.domain")}
    finally:
        conn.close()
    return jsonify({
        "beliefs": [dict(r) for r in rows],
        "count": len(rows),
        "domains": _DOMAINS,
        "kinds": kinds,
        "domain_counts": dcounts,
    })


@beliefs_admin_bp.route("/api/beliefs/trends")
def beliefs_trends():
    """v2 belief movement over a recent window (read straight from belief_v2_live.db):
      - trending_up      : net-positive real-signal beliefs (gaining ground)
      - challenged       : real-signal challenges >= confirms (losing ground / contested)
      - recently_changed : beliefs that left the active set (faded to dormant) in the window
    Rows open the same edit drawer as Browse, so the evidence trail is shared."""
    days = _int_arg("days", _TREND_WINDOW_DAYS)
    min_net = _int_arg("min_net", _TREND_MIN_NET)
    limit = _int_arg("limit", _TREND_LIMIT)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    src_ph = ",".join("?" for _ in _REAL_SOURCES)

    windowed = (
        " SELECT b.belief_id, COALESCE(o.statement_override, b.statement_nl) AS statement,"
        "        c.domain, b.kind, b.status, b.obs_count, b.last_observed,"
        "        SUM(CASE WHEN ob.polarity='affirm' THEN 1 ELSE 0 END) AS confirms,"
        "        SUM(CASE WHEN ob.polarity='contradict' THEN 1 ELSE 0 END) AS challenges"
        " FROM beliefs b"
        " JOIN observations ob ON ob.resolves_to = b.belief_id"
        " LEFT JOIN belief_category c ON c.belief_id = b.belief_id"
        " LEFT JOIN belief_user_override o ON o.belief_id = b.belief_id"
        " WHERE b.status='active' AND COALESCE(o.suppressed,0)=0"
        f"   AND ob.occurred_at >= ? AND ob.source IN ({src_ph})"
        " GROUP BY b.belief_id"
    )
    conn = _connect()
    try:
        trending_up = conn.execute(
            windowed + " HAVING (confirms - challenges) >= ?"
                       " ORDER BY (confirms - challenges) DESC, confirms DESC LIMIT ?",
            [cutoff, *_REAL_SOURCES, min_net, limit]).fetchall()
        # Challenged = engine-flagged contested beliefs (v2's native dispute signal, all-time)
        # plus active beliefs that lost ground in the window; contested first, deduped.
        losing = conn.execute(
            windowed + " HAVING challenges > 0 AND challenges >= confirms"
                       " ORDER BY (challenges - confirms) DESC, challenges DESC LIMIT ?",
            [cutoff, *_REAL_SOURCES, limit]).fetchall()
        contested = conn.execute(
            " SELECT b.belief_id, COALESCE(o.statement_override, b.statement_nl) AS statement,"
            "        c.domain, b.kind, b.status, b.obs_count, b.last_observed,"
            "        SUM(CASE WHEN ob.polarity='affirm' THEN 1 ELSE 0 END) AS confirms,"
            "        SUM(CASE WHEN ob.polarity='contradict' THEN 1 ELSE 0 END) AS challenges"
            " FROM beliefs b JOIN observations ob ON ob.resolves_to = b.belief_id"
            " LEFT JOIN belief_category c ON c.belief_id = b.belief_id"
            " LEFT JOIN belief_user_override o ON o.belief_id = b.belief_id"
            " WHERE b.status='contested' AND COALESCE(o.suppressed,0)=0"
            " GROUP BY b.belief_id ORDER BY challenges DESC LIMIT ?", [limit]).fetchall()
        seen, challenged = set(), []
        for r in [*contested, *losing]:
            if r["belief_id"] in seen:
                continue
            seen.add(r["belief_id"])
            challenged.append(r)
        challenged = challenged[:limit]
        recently_changed = conn.execute(
            " SELECT b.belief_id, COALESCE(o.statement_override, b.statement_nl) AS statement,"
            "        c.domain, b.kind, b.status, b.obs_count, b.last_observed AS changed_on"
            " FROM beliefs b"
            " LEFT JOIN belief_category c ON c.belief_id = b.belief_id"
            " LEFT JOIN belief_user_override o ON o.belief_id = b.belief_id"
            " WHERE b.status='dormant' AND b.last_observed >= ?"
            " ORDER BY b.last_observed DESC LIMIT ?", [cutoff, limit]).fetchall()
    finally:
        conn.close()
    return jsonify({
        "window_days": days,
        "trending_up": [dict(r) for r in trending_up],
        "challenged": [dict(r) for r in challenged],
        "recently_changed": [dict(r) for r in recently_changed],
    })


@beliefs_admin_bp.route("/api/beliefs/item")
def beliefs_item():
    """One belief's full state + its observation evidence trail (the 'why')."""
    bid = (request.args.get("belief_id") or "").strip()
    if not bid:
        return jsonify({"error": "belief_id required"}), 400
    conn = _connect()
    try:
        b = conn.execute("SELECT * FROM beliefs WHERE belief_id=?", (bid,)).fetchone()
        if b is None:
            return jsonify({"error": "not found"}), 404
        evidence = conn.execute(
            "SELECT occurred_at, recorded_at, source, polarity, strength, raw_text "
            "FROM observations WHERE resolves_to=? ORDER BY recorded_at DESC LIMIT 60", (bid,)).fetchall()
        dom = conn.execute("SELECT domain FROM belief_category WHERE belief_id=?", (bid,)).fetchone()
        ovr = conn.execute("SELECT * FROM belief_user_override WHERE belief_id=?", (bid,)).fetchone()
        sid = conn.execute("SELECT short_id FROM belief_short_id WHERE belief_id=?", (bid,)).fetchone()
        btags = [r[0] for r in conn.execute("SELECT tag FROM belief_tags WHERE belief_id=? ORDER BY tag", (bid,))]
    finally:
        conn.close()
    return jsonify({
        "belief": dict(b),
        "short_id": f"b{sid['short_id']}" if sid else None,
        "domain": dom["domain"] if dom else None,
        "tags": btags,
        "override": dict(ovr) if ovr else None,
        "evidence": [dict(e) for e in evidence],
    })


@beliefs_admin_bp.route("/api/beliefs/update", methods=["POST"])
def beliefs_update():
    """Apply owner corrections as durable overrides. JSON body:
    ``{belief_id, domain?, suppressed?, locked?, statement?}`` — omitted keys are
    left unchanged. domain writes belief_category; suppress/lock/statement write
    belief_user_override (merged with any existing override)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    bid = (data.get("belief_id") or "").strip()
    if not bid:
        return jsonify({"error": "belief_id required"}), 400

    conn = _connect()
    try:
        if not conn.execute("SELECT 1 FROM beliefs WHERE belief_id=?", (bid,)).fetchone():
            return jsonify({"error": "not found"}), 404

        if "domain" in data:
            d = (data.get("domain") or "").strip()
            if d not in _DOMAINS:
                return jsonify({"error": f"invalid domain {d!r}"}), 400
            conn.execute("INSERT OR REPLACE INTO belief_category(belief_id, domain) VALUES(?,?)", (bid, d))

        if any(k in data for k in ("suppressed", "locked", "statement")):
            cur = conn.execute(
                "SELECT suppressed, locked, statement_override FROM belief_user_override WHERE belief_id=?",
                (bid,)).fetchone()
            supp = int(cur["suppressed"]) if cur else 0
            lock = int(cur["locked"]) if cur else 0
            stmt = cur["statement_override"] if cur else None
            if "suppressed" in data:
                supp = 1 if data.get("suppressed") else 0
            if "locked" in data:
                lock = 1 if data.get("locked") else 0
            if "statement" in data:
                s = (data.get("statement") or "").strip()
                stmt = s or None
            conn.execute(
                "INSERT OR REPLACE INTO belief_user_override"
                "(belief_id, suppressed, locked, statement_override, updated_at) VALUES(?,?,?,?,?)",
                (bid, supp, lock, stmt, datetime.now(timezone.utc).isoformat()))

        if "tags" in data:
            clean = belief_tags_mod.sanitize(data.get("tags") or [])
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("DELETE FROM belief_tags WHERE belief_id=?", (bid,))
            conn.executemany(
                "INSERT OR IGNORE INTO belief_tags (belief_id, tag, assigned_at, method) VALUES (?,?,?,?)",
                [(bid, t, now_iso, "manual") for t in clean])
        conn.commit()

        row = conn.execute(
            "SELECT b.belief_id, COALESCE(o.statement_override, b.statement_nl) AS statement,"
            " (o.statement_override IS NOT NULL) AS edited, b.kind, b.status, b.obs_count, b.last_observed,"
            " c.domain, COALESCE(o.suppressed,0) AS suppressed, COALESCE(o.locked,0) AS locked"
            " FROM beliefs b LEFT JOIN belief_category c ON c.belief_id=b.belief_id"
            " LEFT JOIN belief_user_override o ON o.belief_id=b.belief_id WHERE b.belief_id=?", (bid,)).fetchone()
    finally:
        conn.close()
    logger.info("[beliefs] corrected %s: %s", bid, sorted(k for k in data if k != "belief_id"))
    return jsonify({"success": True, "belief": dict(row)})
