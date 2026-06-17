"""Owner-only /beliefs management page + API — browse and correct the v1 belief set.

Local-only surface (``reject_if_not_local``) over the v1 belief engine (emi.db:
user_beliefs + belief_evidence + belief_tags + belief_short_id). Lists every live belief
with domain / kind / status / strength + tags and lets the owner CORRECT it: edit the
statement, reclassify domain, retag, suppress (deprecate), or LOCK it against the nightly
auto-revision.

v1 rows are MUTABLE (not a rebuilt projection), so edits are DIRECT row updates — no override
side tables. The lock is the durability mechanism: a locked belief is skipped by the updater,
decay, and canonicalize (see belief_engine: update_beliefs / decay.recompute / canonicalize),
so a correction here sticks.

Deprecated beliefs are evicted nightly to user_beliefs_archive / belief_evidence_archive
(belief_archive routine); `include_archived` also reads those for browsing/provenance, but
edits apply only to live beliefs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import yaml
from flask import Blueprint, jsonify, render_template, request

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir, get_repo_root
from app.routes._security import reject_if_not_local

logger = get_logger(__name__)

beliefs_admin_bp = Blueprint("beliefs_admin", __name__)
# Owner-only: full belief set with evidence + edit controls; never a proxied request.
beliefs_admin_bp.before_request(reject_if_not_local)

# Trends read the evidence log directly. Only real user-driven signal counts toward movement —
# the engine's own bookkeeping (canonicalization, deprecation, seed replay) is excluded so the
# numbers reflect what the user actually did.
_REAL_SOURCES = ("daily_insights", "ticket_acceptance", "ticket_rejection", "user_comment")
_POS = ("confirms",)
_NEG = ("contradicts", "rejects")
_TREND_WINDOW_DAYS = 21
_TREND_MIN_NET = 2
_TREND_LIMIT = 15


def _db_path() -> Path:
    return Path(get_repo_root()) / "emi.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # The archive tables are created by the belief_archive routine on its first run; ensure them
    # (empty) so queries that read them work before that sweep has ever run. No-op once they exist.
    conn.execute("CREATE TABLE IF NOT EXISTS user_beliefs_archive AS SELECT * FROM user_beliefs WHERE 0")
    conn.execute("CREATE TABLE IF NOT EXISTS belief_evidence_archive AS SELECT * FROM belief_evidence WHERE 0")
    return conn


def _domains(conn: sqlite3.Connection) -> List[str]:
    """Domain vocabulary for the filters + the reclassify dropdown — the union of the
    configured domains and any actually present, so nothing is unreachable."""
    present = {r[0] for r in conn.execute("SELECT DISTINCT domain FROM user_beliefs WHERE domain IS NOT NULL")}
    try:
        cfg = yaml.safe_load((get_configs_dir() / "belief_domains.yaml").read_text(encoding="utf-8")) or {}
        configured = set((cfg.get("domains") or {}).keys()) if isinstance(cfg.get("domains"), dict) else set()
    except Exception:
        configured = set()
    return sorted(present | configured)


def _tags_vocab() -> List[str]:
    try:
        cfg = yaml.safe_load((get_configs_dir() / "belief_tags.yaml").read_text(encoding="utf-8")) or {}
        tags = cfg.get("tags", cfg)
        return sorted(tags.keys()) if isinstance(tags, dict) else sorted(tags)
    except Exception:
        logger.warning("[beliefs] could not load belief_tags.yaml vocab", exc_info=True)
        return []


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


# Column projection shared by list + item, aliased to the field names the page reads.
_SELECT = (
    "SELECT b.id AS belief_id, b.statement, b.domain, b.kind, b.status, b.confidence, b.locked, "
    " b.observation_count AS obs_count, b.last_confirmed AS last_observed, b.first_observed, "
    " ROUND(COALESCE(b.current_net_weight, 0), 1) AS net, b.current_confidence_band AS band, "
    " ('b' || s.short_id) AS short_id, "
    " (SELECT GROUP_CONCAT(tag) FROM belief_tags WHERE belief_id = b.id) AS tags "
)


@beliefs_admin_bp.route("/beliefs")
def beliefs_page():
    conn = _connect()
    try:
        return render_template("beliefs.html", domains=_domains(conn), tags_vocab=_tags_vocab())
    finally:
        conn.close()


@beliefs_admin_bp.route("/api/beliefs/list")
def beliefs_list():
    """Live beliefs (filterable). `include_archived=1` also reads the archive tables (read-only,
    for provenance browsing). Filtering is server-side so big result sets stay light."""
    domain = (request.args.get("domain") or "").strip()
    kind = (request.args.get("kind") or "").strip()
    status = (request.args.get("status") or "active").strip()
    q = (request.args.get("q") or "").strip().lower()
    include_archived = request.args.get("include_archived") == "1"

    where, params = [], []
    if status == "active":
        where.append("b.status = 'active'")
    elif status == "contested":
        where.append("b.status = 'contested'")
    # status == "all": no status filter (live = active + contested).
    if domain:
        where.append("b.domain = ?"); params.append(domain)
    if kind:
        where.append("b.kind = ?"); params.append(kind)
    if q:
        where.append("lower(b.statement) LIKE ?"); params.append(f"%{q}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = " ORDER BY b.observation_count DESC, b.id"

    conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute(
            _SELECT + " FROM user_beliefs b LEFT JOIN belief_short_id s ON s.belief_id = b.id"
            + clause + order, params)]
        for r in rows:
            r["archived"] = 0
        if include_archived:
            arch_where = [w for w in where if not w.startswith("b.status")]  # archive is all-deprecated
            arch_clause = (" WHERE " + " AND ".join(arch_where)) if arch_where else ""
            arch_params = [p for w, p in zip(where, params) if not w.startswith("b.status")] if where else []
            arch = [dict(r) for r in conn.execute(
                _SELECT + " FROM user_beliefs_archive b LEFT JOIN belief_short_id s ON s.belief_id = b.id"
                + arch_clause + order, arch_params)]
            for r in arch:
                r["archived"] = 1
            rows += arch
        kinds = [r[0] for r in conn.execute(
            "SELECT DISTINCT kind FROM user_beliefs WHERE kind IS NOT NULL ORDER BY 1")]
        dcounts = {r[0]: r[1] for r in conn.execute(
            "SELECT domain, COUNT(*) FROM user_beliefs WHERE status='active' GROUP BY domain")}
        domains = _domains(conn)
    finally:
        conn.close()
    return jsonify({"beliefs": rows, "count": len(rows), "domains": domains,
                    "kinds": kinds, "domain_counts": dcounts})


@beliefs_admin_bp.route("/api/beliefs/item")
def beliefs_item():
    """One belief's full state + its evidence trail (the 'why')."""
    bid = (request.args.get("belief_id") or "").strip()
    if not bid:
        return jsonify({"error": "belief_id required"}), 400
    conn = _connect()
    try:
        b = conn.execute("SELECT * FROM user_beliefs WHERE id=?", (bid,)).fetchone()
        ev_table, archived = "belief_evidence", 0
        if b is None:
            b = conn.execute("SELECT * FROM user_beliefs_archive WHERE id=?", (bid,)).fetchone()
            ev_table, archived = "belief_evidence_archive", 1
        if b is None:
            return jsonify({"error": "not found"}), 404
        evidence = [dict(e) for e in conn.execute(
            f"SELECT source_type, source_date, signal_type, summary, raw_text, weight "
            f"FROM {ev_table} WHERE belief_id=? ORDER BY source_date DESC, created_at DESC LIMIT 80", (bid,))]
        sid = conn.execute("SELECT short_id FROM belief_short_id WHERE belief_id=?", (bid,)).fetchone()
        tags = [r[0] for r in conn.execute(
            "SELECT tag FROM belief_tags WHERE belief_id=? ORDER BY tag", (bid,))]
    finally:
        conn.close()
    return jsonify({
        "belief": dict(b),
        "short_id": f"b{sid['short_id']}" if sid else None,
        "domain": b["domain"],
        "tags": tags,
        "archived": archived,
        "evidence": evidence,
    })


@beliefs_admin_bp.route("/api/beliefs/update", methods=["POST"])
def beliefs_update():
    """Apply owner corrections directly to the live belief row. JSON body:
    ``{belief_id, statement?, domain?, suppressed?, locked?, tags?}`` — omitted keys unchanged.
    suppress -> status='deprecated' (evicted to the archive on the next sweep); lock -> locked=1
    so the engine can't re-evolve/deprecate it. Edits apply to LIVE beliefs only."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    bid = (data.get("belief_id") or "").strip()
    if not bid:
        return jsonify({"error": "belief_id required"}), 400

    sets, params = [], []
    if "statement" in data:
        s = (data.get("statement") or "").strip()
        if not s:
            return jsonify({"error": "statement cannot be empty"}), 400
        sets.append("statement = ?"); params.append(s)
    if "domain" in data:
        d = (data.get("domain") or "").strip()
        if not d:
            return jsonify({"error": "invalid domain"}), 400
        sets.append("domain = ?"); params.append(d)
    if "locked" in data:
        sets.append("locked = ?"); params.append(1 if data.get("locked") else 0)
    if "suppressed" in data:
        # Suppress = deprecate; un-suppress = reactivate. The nightly archive sweep evicts
        # deprecated rows, so suppression removes the belief from the live set.
        sets.append("status = ?"); params.append("deprecated" if data.get("suppressed") else "active")

    conn = _connect()
    try:
        if not conn.execute("SELECT 1 FROM user_beliefs WHERE id=?", (bid,)).fetchone():
            return jsonify({"error": "not found (archived beliefs are read-only)"}), 404
        if sets:
            sets.append("updated_at = ?"); params.append(datetime.now(timezone.utc).isoformat())
            conn.execute(f"UPDATE user_beliefs SET {', '.join(sets)} WHERE id=?", (*params, bid))
        if "tags" in data:
            now_iso = datetime.now(timezone.utc).isoformat()
            clean = sorted({str(t).strip().lower() for t in (data.get("tags") or []) if str(t).strip()})
            conn.execute("DELETE FROM belief_tags WHERE belief_id=?", (bid,))
            conn.executemany(
                "INSERT OR IGNORE INTO belief_tags (belief_id, tag, assigned_at, method) VALUES (?,?,?,?)",
                [(bid, t, now_iso, "manual") for t in clean])
        conn.commit()
        row = dict(conn.execute(
            _SELECT + " FROM user_beliefs b LEFT JOIN belief_short_id s ON s.belief_id=b.id WHERE b.id=?",
            (bid,)).fetchone())
    finally:
        conn.close()
    logger.info("[beliefs] corrected %s: %s", bid, sorted(k for k in data if k != "belief_id"))
    return jsonify({"success": True, "belief": row})


@beliefs_admin_bp.route("/api/beliefs/trends")
def beliefs_trends():
    """Belief movement over a recent window, read from belief_evidence (richer than v2's
    affirm/contradict log — confirms/qualifies/contradicts/rejects + weight):
      - trending_up      : net-positive real-signal beliefs (gaining ground)
      - challenged       : contested, or real-signal challenges >= confirms (losing ground)
      - recently_changed : beliefs that faded (deprecated) in the window — organic fades only,
                           merge-losers (belief_merges) excluded so it's not merge noise."""
    days = _int_arg("days", _TREND_WINDOW_DAYS)
    min_net = _int_arg("min_net", _TREND_MIN_NET)
    limit = _int_arg("limit", _TREND_LIMIT)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    src_ph = ",".join("?" for _ in _REAL_SOURCES)
    pos_ph = ",".join("?" for _ in _POS)
    neg_ph = ",".join("?" for _ in _NEG)

    # Per active belief, confirms vs challenges from REAL sources in the window.
    windowed = (
        " SELECT b.id AS belief_id, b.statement, b.domain, b.kind, b.status,"
        "        b.observation_count AS obs_count, b.last_confirmed AS last_observed,"
        f"       SUM(CASE WHEN e.signal_type IN ({pos_ph}) THEN 1 ELSE 0 END) AS confirms,"
        f"       SUM(CASE WHEN e.signal_type IN ({neg_ph}) THEN 1 ELSE 0 END) AS challenges"
        "  FROM user_beliefs b JOIN belief_evidence e ON e.belief_id = b.id"
        "  WHERE b.status IN ('active','contested')"
        f"   AND e.source_date >= ? AND e.source_type IN ({src_ph})"
        "  GROUP BY b.id"
    )
    win_params = [*_POS, *_NEG, cutoff, *_REAL_SOURCES]

    conn = _connect()
    try:
        trending_up = [dict(r) for r in conn.execute(
            windowed + " HAVING (confirms - challenges) >= ?"
                       " ORDER BY (confirms - challenges) DESC, confirms DESC LIMIT ?",
            [*win_params, min_net, limit])]
        losing = [dict(r) for r in conn.execute(
            windowed + " HAVING challenges > 0 AND challenges >= confirms"
                       " ORDER BY (challenges - confirms) DESC, challenges DESC LIMIT ?",
            [*win_params, limit])]
        contested = [dict(r) for r in conn.execute(
            _SELECT + " FROM user_beliefs b LEFT JOIN belief_short_id s ON s.belief_id=b.id"
            " WHERE b.status='contested' ORDER BY b.observation_count DESC LIMIT ?", [limit])]
        seen, challenged = set(), []
        for r in [*contested, *losing]:
            if r["belief_id"] in seen:
                continue
            seen.add(r["belief_id"])
            challenged.append(r)
        challenged = challenged[:limit]
        # Recently faded: deprecated in the window, organic fades only (exclude merge-losers).
        # Deprecated rows live briefly in user_beliefs (pre-sweep) then in the archive.
        recently_changed = [dict(r) for r in conn.execute(
            " SELECT belief_id, statement, domain, changed_on FROM ("
            "   SELECT id AS belief_id, statement, domain, updated_at AS changed_on FROM user_beliefs WHERE status='deprecated'"
            "   UNION ALL"
            "   SELECT id AS belief_id, statement, domain, updated_at AS changed_on FROM user_beliefs_archive"
            " ) WHERE changed_on >= ? AND belief_id NOT IN (SELECT loser_id FROM belief_merges)"
            " ORDER BY changed_on DESC LIMIT ?", [cutoff, limit])]
    finally:
        conn.close()
    return jsonify({
        "window_days": days,
        "trending_up": trending_up,
        "challenged": challenged,
        "recently_changed": recently_changed,
    })
