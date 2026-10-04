"""Owner-only /beliefs management page + API — browse and correct the live belief catalog.

Local-only surface (``reject_if_not_local``) over the belief intake's store (emi.db:
belief_intake_beliefs / _evidence / _revisions + belief_tags), the live catalog since the
2026-09-29 cutover. Lists every belief with its kind, tags, evidence counts and refinement
parent, shows its evidence and revision history, and lets the owner correct it.

Corrections follow the intake's own rules — a belief is a view of its evidence:
- a new statement is a REVISION (old wording kept in the history) plus an evidence row carrying
  the owner's words (kind "said", source_ref "owner"), so later revisions weigh the correction
  as the newest thing the owner said;
- retire / restore flips status and records it in the revision history (nothing is deleted);
- tags written here are method 'manual', which the nightly tagger never overwrites.
After a write the export (resource_user_beliefs.json) is rewritten so its readers see it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

import yaml
from flask import Blueprint, jsonify, render_template, request

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir
from app.routes._security import reject_if_not_local

logger = get_logger(__name__)

beliefs_admin_bp = Blueprint("beliefs_admin", __name__)
# Owner-only: full belief set with evidence + edit controls; never a proxied request.
beliefs_admin_bp.before_request(reject_if_not_local)

_P = "belief_intake_"
_TREND_WINDOW_DAYS = 21
_TREND_MIN_NET = 2
_TREND_LIMIT = 15


def _read():
    from belief_engine.intake.store import app_db
    return app_db()(False)


def _write():
    from belief_engine.intake.store import app_db
    return app_db()(True)


def _store():
    from belief_engine.intake.run import app_store
    return app_store()


def _today() -> str:
    from app.assistant.utils.time_utils import get_local_time
    return get_local_time().date().isoformat()


def _tags_vocab() -> List[str]:
    cfg = yaml.safe_load((get_configs_dir() / "belief_tags.yaml").read_text(encoding="utf-8")) or {}
    return sorted((cfg.get("tags") or {}).keys())


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


# Every belief with its tags and evidence counts, aliased to the field names the page reads.
_SELECT = (
    f"SELECT b.id AS belief_id, b.statement, b.kind, b.scope, b.status, b.parent_id, b.created_day, "
    f" b.calendar, b.calendar_note, b.review, "
    f" (SELECT GROUP_CONCAT(tag) FROM belief_tags t WHERE t.belief_id = b.id) AS tags, "
    f" (SELECT COUNT(*) FROM {_P}evidence e WHERE e.belief_id = b.id AND e.relation='support') AS support, "
    f" (SELECT COUNT(*) FROM {_P}evidence e WHERE e.belief_id = b.id AND e.relation='contradict') AS contradict, "
    f" (SELECT MAX(day) FROM {_P}evidence e WHERE e.belief_id = b.id AND e.relation='support') AS last_observed "
    f"FROM {_P}beliefs b"
)


def _row(c, belief_id: str) -> Dict[str, Any] | None:
    r = c.execute(_SELECT + " WHERE b.id = ?", (belief_id,)).fetchone()
    return dict(r) if r else None


@beliefs_admin_bp.route("/beliefs")
def beliefs_page():
    return render_template("beliefs.html", tags_vocab=_tags_vocab())


@beliefs_admin_bp.route("/api/beliefs/list")
def beliefs_list():
    """Beliefs filtered by status (active | retired | all), tag, kind and statement text."""
    tag = (request.args.get("tag") or "").strip()
    kind = (request.args.get("kind") or "").strip()
    status = (request.args.get("status") or "active").strip()
    q = (request.args.get("q") or "").strip().lower()

    where, params = [], []
    if status in ("active", "retired"):
        where.append("b.status = ?"); params.append(status)
    if tag:
        where.append("b.id IN (SELECT belief_id FROM belief_tags WHERE tag = ?)"); params.append(tag)
    if kind:
        where.append("b.kind = ?"); params.append(kind)
    if q:
        where.append("lower(b.statement) LIKE ?"); params.append(f"%{q}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with _read() as c:
        rows = [dict(r) for r in c.execute(
            _SELECT + clause + " ORDER BY support DESC, CAST(SUBSTR(b.id, 2) AS INTEGER)", params)]
        kinds = [r[0] for r in c.execute(f"SELECT DISTINCT kind FROM {_P}beliefs ORDER BY 1")]
        tag_counts = {r[0]: r[1] for r in c.execute(
            f"SELECT t.tag, COUNT(*) FROM belief_tags t JOIN {_P}beliefs b ON b.id = t.belief_id "
            "WHERE b.status = 'active' GROUP BY t.tag")}
    return jsonify({"beliefs": rows, "count": len(rows), "kinds": kinds, "tag_counts": tag_counts})


@beliefs_admin_bp.route("/api/beliefs/item")
def beliefs_item():
    """One belief's full state + its evidence trail (the 'why') + its revision history."""
    bid = (request.args.get("belief_id") or "").strip()
    if not bid:
        return jsonify({"error": "belief_id required"}), 400
    with _read() as c:
        b = _row(c, bid)
        if b is None:
            return jsonify({"error": "not found"}), 404
        evidence = [dict(e) for e in c.execute(
            f"SELECT day, time, kind, relation, text, in_reply_to, source_ref, via FROM {_P}evidence "
            "WHERE belief_id = ? ORDER BY day DESC, time DESC, id DESC", (bid,))]
        revisions = [dict(r) for r in c.execute(
            f"SELECT day, old_statement, new_statement, old_kind, new_kind, reasoning FROM {_P}revisions "
            "WHERE belief_id = ? ORDER BY id DESC", (bid,))]
        parent = _row(c, b["parent_id"]) if b["parent_id"] else None
        children = [dict(r) for r in c.execute(
            f"SELECT id AS belief_id, statement, status FROM {_P}beliefs WHERE parent_id = ? ORDER BY rowid", (bid,))]
        tags = [r[0] for r in c.execute("SELECT tag FROM belief_tags WHERE belief_id = ? ORDER BY tag", (bid,))]
    return jsonify({"belief": b, "tags": tags, "evidence": evidence, "revisions": revisions,
                    "parent": parent, "children": children})


@beliefs_admin_bp.route("/api/beliefs/update", methods=["POST"])
def beliefs_update():
    """Owner corrections. JSON body ``{belief_id, statement?, retired?, tags?, calendar?}`` — omitted keys
    unchanged. See the module docstring for what each correction writes."""
    from belief_engine.export.export_beliefs import export_beliefs
    from belief_engine.tagging import sanitize

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    bid = (data.get("belief_id") or "").strip()
    if not bid:
        return jsonify({"error": "belief_id required"}), 400
    store = _store()
    current = store.get(bid)
    if current is None:
        return jsonify({"error": "not found"}), 404
    day = _today()

    if "statement" in data:
        statement = (data.get("statement") or "").strip()
        if not statement:
            return jsonify({"error": "statement cannot be empty"}), 400
        _correct_statement(store, bid, current, statement, day)
    if "calendar" in data:
        if data["calendar"] not in ("approved", "declined"):
            return jsonify({"error": "calendar must be approved or declined"}), 400
        with _read() as c:
            note = c.execute(f"SELECT calendar_note FROM {_P}beliefs WHERE id = ?", (bid,)).fetchone()[0]
        store.set_calendar(bid, data["calendar"], note)
    if "retired" in data:
        want = "retired" if data.get("retired") else "active"
        if want != current["status"]:
            if want == "retired":
                store.retire(bid, day, "owner retired it in /beliefs")
            else:
                store.restore(bid, day, "owner restored it in /beliefs")
    if "tags" in data:
        from datetime import datetime, timezone
        clean = sanitize(data.get("tags") or [])
        now_iso = datetime.now(timezone.utc).isoformat()
        with _write() as c:
            c.execute("DELETE FROM belief_tags WHERE belief_id = ?", (bid,))
            c.executemany("INSERT INTO belief_tags (belief_id, tag, assigned_at, method) VALUES (?,?,?,?)",
                          [(bid, t, now_iso, "manual") for t in clean])

    export_beliefs()
    with _read() as c:
        row = _row(c, bid)
    logger.info("[beliefs] corrected %s: %s", bid, sorted(k for k in data if k != "belief_id"))
    return jsonify({"success": True, "belief": row})


def _correct_statement(store, bid: str, current: Dict[str, Any], statement: str, day: str) -> None:
    """The owner's rewording: a revision, and the owner's words as the newest evidence."""
    if statement == current["statement"]:
        return
    from app.assistant.embeddings.embedder import embed_texts
    store.revise(bid, day, {"statement": statement, "kind": current["kind"],
                            "reasoning": "owner correction in /beliefs"}, embed_texts([statement])[0])
    store.add_evidence(bid, day, kind="said", relation="support", text=statement,
                       source_ref="owner", via="owner_correction")


# ── The morning review (2026-10-03) ─────────────────────────────────────────────────────────────────────
# Two panels: the beliefs proposed as calendar items (the nightly tagger, belief_engine/intake/calendar.py),
# and the other beliefs formed since the last review. A calendar decision also reviews the belief.

_REVIEW_ACTIONS = ("add_to_calendar", "not_for_calendar", "approve", "dismiss", "edit", "keep_new", "keep_old",
                   "keep_both", "apply_rewrite")
_KEEP = {"keep_new": "new", "keep_old": "old", "keep_both": "both"}


def _evidence(c, belief_id: str) -> List[Dict[str, Any]]:
    return [dict(e) for e in c.execute(
        f"SELECT day, time, kind, relation, text, in_reply_to, source_ref FROM {_P}evidence WHERE belief_id = ? "
        "ORDER BY day DESC, time DESC, id DESC", (belief_id,))]


@beliefs_admin_bp.route("/beliefs/review")
def beliefs_review_page():
    return render_template("beliefs_review.html")


@beliefs_admin_bp.route("/api/beliefs/review")
def beliefs_review_list():
    """{calendar: beliefs proposed as calendar items, new: the other unreviewed beliefs (active ones only),
    contradictions: each held belief with its evidence and the held beliefs it contradicts, with theirs}."""
    import json
    with _read() as c:
        calendar = [dict(r) for r in c.execute(
            _SELECT + " WHERE b.status = 'active' AND b.calendar = 'proposed' ORDER BY CAST(SUBSTR(b.id, 2) AS INTEGER)")]
        new = [dict(r) for r in c.execute(
            _SELECT + " WHERE b.status = 'active' AND b.review IS NULL AND COALESCE(b.calendar, '') != 'proposed' "
            "ORDER BY CAST(SUBSTR(b.id, 2) AS INTEGER)")]
        contradictions = []
        for r in c.execute(_SELECT + " WHERE b.status = 'pending' ORDER BY CAST(SUBSTR(b.id, 2) AS INTEGER)").fetchall():
            held = dict(r)
            raw_targets, raw_drafts = c.execute(f"SELECT contradicts, drafts FROM {_P}beliefs WHERE id = ?",
                                                (held["belief_id"],)).fetchone()
            drafts = json.loads(raw_drafts or "{}")
            contradictions.append({**held, "evidence": _evidence(c, held["belief_id"]),
                                   "contradicts": [{**_row(c, t), "evidence": _evidence(c, t),
                                                    "draft": (drafts.get(t) or {}).get("statement")}
                                                   for t in json.loads(raw_targets)]})
    return jsonify({"calendar": calendar, "new": new, "contradictions": contradictions})


@beliefs_admin_bp.route("/api/beliefs/review/decide", methods=["POST"])
def beliefs_review_decide():
    """One review decision. JSON body ``{belief_id, action, statement?}``:
    add_to_calendar / not_for_calendar: the owner's calendar decision (the belief counts as reviewed);
    approve: reviewed as it is; dismiss: retired and reviewed; edit: reworded (``statement``) as an owner
    correction, and reviewed; a proposed calendar item stays proposed until it is decided, and a held
    contradiction stays held; keep_new / keep_old / keep_both: the owner's decision on a held contradiction
    (IntakeStore.resolve_contradiction); apply_rewrite: the drafted rewrites applied, ``rewrites`` {id: statement}
    carrying the owner's edits of them."""
    from belief_engine.export.export_beliefs import export_beliefs
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    bid, action = (data.get("belief_id") or "").strip(), data.get("action")
    if not bid or action not in _REVIEW_ACTIONS:
        return jsonify({"error": f"belief_id and an action of {list(_REVIEW_ACTIONS)} required"}), 400
    store = _store()
    current = store.get(bid)
    if current is None:
        return jsonify({"error": "not found"}), 404
    day = _today()
    with _read() as c:
        note = c.execute(f"SELECT calendar_note FROM {_P}beliefs WHERE id = ?", (bid,)).fetchone()[0]
    if action == "apply_rewrite":
        rewrites = {str(k): str(v or "").strip() for k, v in (data.get("rewrites") or {}).items()}
        if not rewrites or not all(rewrites.values()):
            return jsonify({"error": "apply_rewrite needs the rewritten statements"}), 400
        from app.assistant.embeddings.embedder import embed_texts
        ids = list(rewrites)
        vectors = embed_texts([rewrites[i] for i in ids])
        try:
            store.resolve_contradiction(bid, "rewrite", day, {i: (rewrites[i], v) for i, v in zip(ids, vectors)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    elif action in _KEEP:
        try:
            store.resolve_contradiction(bid, _KEEP[action], day)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    elif action == "add_to_calendar":
        store.set_calendar(bid, "approved", note)
        store.set_review(bid, "approved", day)
    elif action == "not_for_calendar":
        store.set_calendar(bid, "declined", note)
        store.set_review(bid, "approved", day)
    elif action == "approve":
        store.set_review(bid, "approved", day)
    elif action == "dismiss":
        store.retire(bid, day, "owner dismissed it in the morning review")
        store.set_review(bid, "dismissed", day)
    else:
        statement = (data.get("statement") or "").strip()
        if not statement:
            return jsonify({"error": "statement cannot be empty"}), 400
        _correct_statement(store, bid, current, statement, day)
        if current["status"] != "pending":
            store.set_review(bid, "edited", day)
    export_beliefs()
    with _read() as c:
        row = _row(c, bid)
    logger.info("[beliefs] review %s: %s", bid, action)
    return jsonify({"success": True, "belief": row})


@beliefs_admin_bp.route("/api/beliefs/trends")
def beliefs_trends():
    """Belief movement over a recent window, from the evidence and revision history:
      - trending_up      : active beliefs whose support outruns contradiction in the window
      - challenged       : active beliefs contradicted at least as often as supported
      - recently_changed : revisions in the window (restated, retired or restored)"""
    from app.assistant.utils.time_utils import get_local_time
    days = _int_arg("days", _TREND_WINDOW_DAYS)
    min_net = _int_arg("min_net", _TREND_MIN_NET)
    limit = _int_arg("limit", _TREND_LIMIT)
    cutoff = (get_local_time().date() - timedelta(days=days)).isoformat()

    windowed = (
        f"SELECT b.id AS belief_id, b.statement, b.kind, "
        f"  SUM(CASE WHEN e.relation='support' THEN 1 ELSE 0 END) AS confirms, "
        f"  SUM(CASE WHEN e.relation='contradict' THEN 1 ELSE 0 END) AS challenges "
        f"FROM {_P}beliefs b JOIN {_P}evidence e ON e.belief_id = b.id "
        f"WHERE b.status = 'active' AND e.day >= ? GROUP BY b.id"
    )
    with _read() as c:
        trending_up = [dict(r) for r in c.execute(
            windowed + " HAVING (confirms - challenges) >= ? ORDER BY (confirms - challenges) DESC, confirms DESC LIMIT ?",
            (cutoff, min_net, limit))]
        challenged = [dict(r) for r in c.execute(
            windowed + " HAVING challenges > 0 AND challenges >= confirms ORDER BY challenges DESC LIMIT ?",
            (cutoff, limit))]
        recently_changed = [dict(r) for r in c.execute(
            f"SELECT r.belief_id, r.new_statement AS statement, r.old_statement, r.reasoning, r.day AS changed_on, "
            f"b.status FROM {_P}revisions r JOIN {_P}beliefs b ON b.id = r.belief_id "
            "WHERE r.day >= ? ORDER BY r.day DESC, r.id DESC LIMIT ?", (cutoff, limit))]
    return jsonify({"window_days": days, "trending_up": trending_up, "challenged": challenged,
                    "recently_changed": recently_changed})
