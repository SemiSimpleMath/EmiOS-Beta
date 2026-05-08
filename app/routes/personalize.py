"""Personalization dashboard routes — gives the user direct visibility +
control over the things that shape Emi's day-to-day behavior:

- Beliefs: trending-up / challenged / recently-deprecated views plus
  per-belief evidence drill-down.
- Directives: edit resource_orchestrator_user_prefs_personal.md (the
  gitignored personal overlay) with live-rendered preview of the final
  concatenated prompt section the planner reads.

Planned sibling page (not yet built — placeholder in the menu):
- Reminder schedule (one-click toggle / retime / delete for
  cron_reminder routines buried in configs/routines.json)

Reads live SQLite for beliefs (the exported JSON has no trend signal).
For directives, reads / writes the resource files directly and reloads
the resource_manager cache so the next agent run sees the new content.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import text as _sql

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)

personalize_bp = Blueprint("personalize", __name__)


# Defaults tuned against the user's belief volume (~528 active beliefs,
# ~120 evidence rows/day). Can be overridden via query params.
_DEFAULT_WINDOW_DAYS = 14
_DEFAULT_MIN_NET_FOR_TRENDING = 3
_DEFAULT_LIMIT = 12


# ── Data layer ────────────────────────────────────────────────────────────────


def _real_evidence_filter() -> str:
    """source_types that represent real signals (not internal audit rows).
    Excludes 'deprecation', 'canonicalization', 'manual_seed', 'decay_review'
    so the trending counts reflect actual user-driven evidence, not the
    bookkeeping the engine writes when it merges/closes beliefs."""
    return "source_type IN ('daily_insights', 'ticket_acceptance', 'ticket_rejection', 'kg_edge')"


def _trending_up(session, days: int, min_net: int, limit: int) -> List[Dict[str, Any]]:
    cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    rows = session.execute(_sql(
        f"""
        SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence,
               b.scope, b.observation_count,
               SUM(CASE WHEN e.signal_type='confirms' THEN 1 ELSE 0 END) AS confirms,
               SUM(CASE WHEN e.signal_type IN ('rejects','contradicts') THEN 1 ELSE 0 END) AS challenges,
               SUM(CASE WHEN e.signal_type='qualifies' THEN 1 ELSE 0 END) AS qualifies
        FROM user_beliefs b
        JOIN belief_evidence e ON e.belief_id = b.id
        WHERE b.status = 'active'
          AND e.source_date >= :cutoff
          AND {_real_evidence_filter()}
        GROUP BY b.id
        HAVING (confirms - challenges) >= :min_net
        ORDER BY (confirms - challenges) DESC, confirms DESC
        LIMIT :limit
        """),
        {"cutoff": cutoff, "min_net": min_net, "limit": limit},
    ).mappings().all()
    return [_belief_dict(r) for r in rows]


def _challenged(session, days: int, limit: int) -> List[Dict[str, Any]]:
    """Active beliefs where real-evidence challenges >= confirms in the
    window and there's at least one challenge. These are the beliefs
    actually losing ground in live signal — the engine doesn't always
    catch them yet (contestation path is dormant), so surfacing here is
    the cheap way to see what to investigate."""
    cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    rows = session.execute(_sql(
        f"""
        SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence,
               b.scope, b.observation_count,
               SUM(CASE WHEN e.signal_type='confirms' THEN 1 ELSE 0 END) AS confirms,
               SUM(CASE WHEN e.signal_type IN ('rejects','contradicts') THEN 1 ELSE 0 END) AS challenges,
               SUM(CASE WHEN e.signal_type='qualifies' THEN 1 ELSE 0 END) AS qualifies
        FROM user_beliefs b
        JOIN belief_evidence e ON e.belief_id = b.id
        WHERE b.status = 'active'
          AND e.source_date >= :cutoff
          AND {_real_evidence_filter()}
        GROUP BY b.id
        HAVING challenges > 0 AND challenges >= confirms
        ORDER BY (challenges - confirms) DESC, challenges DESC
        LIMIT :limit
        """),
        {"cutoff": cutoff, "limit": limit},
    ).mappings().all()
    return [_belief_dict(r) for r in rows]


def _recently_deprecated(session, days: int, limit: int) -> List[Dict[str, Any]]:
    """Deprecation events in the window. The summary on the deprecation/
    canonicalization audit row carries the LLM's reasoning ('merged into
    X because...'), which is the audit trail the user wants to see."""
    cutoff = (utc_now() - timedelta(days=days)).date().isoformat()
    rows = session.execute(_sql(
        """
        SELECT b.id, b.belief_key, b.statement, b.domain, b.confidence,
               b.scope, b.observation_count,
               MAX(e.source_date) AS deprecated_on,
               (
                 SELECT ee.summary FROM belief_evidence ee
                 WHERE ee.belief_id = b.id
                   AND ee.source_type IN ('deprecation', 'canonicalization')
                 ORDER BY ee.source_date DESC LIMIT 1
               ) AS reason
        FROM user_beliefs b
        JOIN belief_evidence e ON e.belief_id = b.id
        WHERE b.status = 'deprecated'
          AND e.source_type IN ('deprecation', 'canonicalization')
          AND e.source_date >= :cutoff
        GROUP BY b.id
        ORDER BY deprecated_on DESC
        LIMIT :limit
        """),
        {"cutoff": cutoff, "limit": limit},
    ).mappings().all()
    out = []
    for r in rows:
        d = _belief_dict(r)
        d["deprecated_on"] = r["deprecated_on"]
        d["reason"] = (r["reason"] or "")
        out.append(d)
    return out


def _evidence_for_belief(session, belief_id: str, limit: int) -> List[Dict[str, Any]]:
    rows = session.execute(_sql(
        """
        SELECT id, source_type, source_date, signal_type, summary, raw_text, weight
        FROM belief_evidence
        WHERE belief_id = :bid
        ORDER BY source_date DESC, id DESC
        LIMIT :limit
        """),
        {"bid": belief_id, "limit": limit},
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "source_type": r["source_type"],
            "source_date": r["source_date"],
            "signal_type": r["signal_type"],
            "summary": r["summary"] or "",
            "raw_text": r["raw_text"] or "",
            "weight": r["weight"],
        }
        for r in rows
    ]


def _belief_dict(row) -> Dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "belief_key": row["belief_key"],
        "statement": row["statement"],
        "domain": row["domain"],
        "confidence": row["confidence"],
        "scope": row["scope"],
        "observation_count": row["observation_count"],
        "confirms": int(row["confirms"]) if "confirms" in keys and row["confirms"] is not None else None,
        "challenges": int(row["challenges"]) if "challenges" in keys and row["challenges"] is not None else None,
        "qualifies": int(row["qualifies"]) if "qualifies" in keys and row["qualifies"] is not None else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────


@personalize_bp.route("/personalize")
def personalize_landing():
    """Landing page — links to the per-area dashboards."""
    return render_template("personalize.html")


@personalize_bp.route("/personalize/beliefs")
def personalize_beliefs():
    return render_template("personalize_beliefs.html")


@personalize_bp.route("/api/personalize/beliefs/trends")
def api_belief_trends():
    """Return three lists: trending_up / challenged / recently_deprecated.

    Optional query params:
      ?days=N              window in days (default 14)
      ?min_net=N           min confirms-challenges for trending_up (default 3)
      ?limit=N             per-section cap (default 12)
    """
    days = int(request.args.get("days") or _DEFAULT_WINDOW_DAYS)
    min_net = int(request.args.get("min_net") or _DEFAULT_MIN_NET_FOR_TRENDING)
    limit = int(request.args.get("limit") or _DEFAULT_LIMIT)

    s = get_session()
    try:
        trending_up = _trending_up(s, days, min_net, limit)
        challenged = _challenged(s, days, limit)
        deprecated = _recently_deprecated(s, days, limit)
    except Exception as e:
        logger.error("[personalize] belief trends failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        s.close()

    return jsonify({
        "success": True,
        "window_days": days,
        "trending_up": trending_up,
        "challenged": challenged,
        "recently_deprecated": deprecated,
    })


@personalize_bp.route("/api/personalize/beliefs/<belief_id>/evidence")
def api_belief_evidence(belief_id: str):
    """Return the recent evidence trail for one belief — what signals
    reached it. Useful for diagnosing 'why is this belief still active'
    or 'why did the engine think this was high-confidence'."""
    limit = int(request.args.get("limit") or 25)

    s = get_session()
    try:
        belief_row = s.execute(_sql(
            "SELECT id, belief_key, statement, domain, confidence, scope, "
            "       status, observation_count, first_observed, last_confirmed "
            "FROM user_beliefs WHERE id = :bid"),
            {"bid": belief_id},
        ).mappings().first()
        if belief_row is None:
            return jsonify({"success": False, "error": "belief not found"}), 404
        belief = dict(belief_row)
        evidence = _evidence_for_belief(s, belief_id, limit)
    except Exception as e:
        logger.error("[personalize] belief evidence failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        s.close()

    return jsonify({
        "success": True,
        "belief": belief,
        "evidence": evidence,
    })


# ── Directives editor ────────────────────────────────────────────────────────
#
# The strategic_planner's "Section 15. User standing directives" reads the
# concatenation of resource_orchestrator_user_prefs.md (public template) and
# resource_orchestrator_user_prefs_personal.md (gitignored overlay). Editing
# the public file is risky — sanitization passes have wiped personal lines
# off it before. The personal overlay is the safe edit surface; this editor
# reads + writes it and shows a live preview of the final concatenation.

_INSTR_DIR = "resources/instructions"
_BASE_FILENAME = "resource_orchestrator_user_prefs.md"
_PERSONAL_FILENAME = "resource_orchestrator_user_prefs_personal.md"

# Tracked-activities config + the agent prompt that decides override/veto.
_TRACKED_REL_PATH = "app/assistant/pipelines/dayflow/step_configs/config_tracked_activities.json"
_AGENT_PROMPT_REL_PATH = "app/assistant/agents/dayflow_cron_tickets/prompts/system.j2"


def _instr_paths() -> Dict[str, Path]:
    root = Path(get_repo_root())
    return {
        "base": root / _INSTR_DIR / _BASE_FILENAME,
        "personal": root / _INSTR_DIR / _PERSONAL_FILENAME,
    }


def _render_combined(base_text: str, personal_text: str) -> str:
    """Same shape as ResourceManager.load_all_from_directory's overlay
    appender. Public template comes first; personal overlay appended
    after a blank line so the LLM treats personal lines as later
    instructions (later wins at LLM read time)."""
    base = (base_text or "").rstrip()
    personal = (personal_text or "").lstrip()
    if not personal:
        return base
    return base + "\n\n" + personal


@personalize_bp.route("/personalize/directives")
def personalize_directives():
    return render_template("personalize_directives.html")


@personalize_bp.route("/api/personalize/directives", methods=["GET"])
def api_directives_get():
    """Return base (public) text + personal overlay text + the rendered
    concatenation that actually reaches the planner prompt."""
    paths = _instr_paths()
    try:
        base_text = paths["base"].read_text(encoding="utf-8") if paths["base"].exists() else ""
        personal_text = paths["personal"].read_text(encoding="utf-8") if paths["personal"].exists() else ""
    except Exception as e:
        logger.error("[personalize] directives read failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({
        "success": True,
        "base": {
            "filename": _BASE_FILENAME,
            "content": base_text,
            "exists": paths["base"].exists(),
        },
        "personal": {
            "filename": _PERSONAL_FILENAME,
            "content": personal_text,
            "exists": paths["personal"].exists(),
        },
        "combined": _render_combined(base_text, personal_text),
    })


@personalize_bp.route("/api/personalize/directives", methods=["POST"])
def api_directives_post():
    """Atomic write of the personal overlay file. Body: {content: str}.

    Path is hardcoded — the editor only ever writes to the personal
    overlay, never to the public template (which would be reverted by
    the next public-repo cleanup pass). After writing, the
    resource_manager picks up the change via mtime-based reload on the
    next prompt build.
    """
    paths = _instr_paths()
    try:
        body = request.get_json(silent=True) or {}
        content = body.get("content")
        if not isinstance(content, str):
            return jsonify({"success": False, "error": "content must be a string"}), 400

        target = paths["personal"]
        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: tempfile in same dir → os.replace. A crash mid-write
        # leaves the previous good file in place rather than a half-written
        # personal overlay (which would corrupt the system prompt).
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(target))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.info(
            "[personalize] saved personal directives overlay (%d chars) to %s",
            len(content), target,
        )

        # Read back so client gets the canonical state plus the rendered
        # combined preview.
        base_text = paths["base"].read_text(encoding="utf-8") if paths["base"].exists() else ""
        personal_text = target.read_text(encoding="utf-8")
        return jsonify({
            "success": True,
            "personal": {
                "filename": _PERSONAL_FILENAME,
                "content": personal_text,
                "exists": True,
            },
            "combined": _render_combined(base_text, personal_text),
        })
    except Exception as e:
        logger.error("[personalize] directives write failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


# ── Tracked Activities (the cron+judgement page) ─────────────────────────────
#
# config_tracked_activities.json defines the BASE cadence per activity
# (threshold minutes, reset triggers, init flags). The dayflow_cron_tickets
# LLM agent reads this every minute, applies its veto/override/stale-window
# policy from prompts/system.j2, and decides whether to actually fire.
# This page exposes the JSON for editing and surfaces a read-only summary +
# raw view of the agent prompt so the user knows what judgement layer sits
# on top of their thresholds.


def _tracked_path() -> Path:
    return Path(get_repo_root()) / _TRACKED_REL_PATH


def _agent_prompt_path() -> Path:
    return Path(get_repo_root()) / _AGENT_PROMPT_REL_PATH


@personalize_bp.route("/personalize/activities")
def personalize_activities():
    return render_template("personalize_activities.html")


@personalize_bp.route("/api/personalize/activities", methods=["GET"])
def api_activities_get():
    """Return the parsed tracked-activities config plus the agent prompt
    (as text) so the page can display the judgement layer's policy
    inline."""
    import json as _json
    tp = _tracked_path()
    ap = _agent_prompt_path()
    try:
        config = _json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {"version": 1, "activities": {}}
        agent_prompt = ap.read_text(encoding="utf-8") if ap.exists() else ""
    except Exception as e:
        logger.error("[personalize] activities read failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({
        "success": True,
        "config": config,
        "config_path": _TRACKED_REL_PATH,
        "agent_prompt": agent_prompt,
        "agent_prompt_path": _AGENT_PROMPT_REL_PATH,
    })


@personalize_bp.route("/api/personalize/activities", methods=["POST"])
def api_activities_post():
    """Atomic write of the tracked-activities config. Body: {config: dict}.

    Writes ONLY to the canonical file path; the path is hardcoded and
    not derived from the request body. Validation:
      - config must be a dict with "activities" (dict of activity defs)
      - per-activity: display_name (str), field_name (str — must match key)
      - threshold (dict with minutes:int, label:str) is optional but if
        present must have those fields
      - calendar_keywords / reset_triggers / etc. are optional but if
        present must be lists
    Anything that fails validation rejects the whole write — prevents a
    half-valid config from breaking the activity_recorder.
    """
    import json as _json
    body = request.get_json(silent=True) or {}
    new_config = body.get("config")
    if not isinstance(new_config, dict):
        return jsonify({"success": False, "error": "body must contain 'config' dict"}), 400
    activities = new_config.get("activities")
    if not isinstance(activities, dict) or not activities:
        return jsonify({"success": False, "error": "'activities' must be a non-empty dict"}), 400

    # Per-activity validation. Reject the whole write on any error so we
    # never write a partially-valid config.
    for key, act in activities.items():
        if not isinstance(act, dict):
            return jsonify({"success": False, "error": f"activity '{key}' must be a dict"}), 400
        for required in ("display_name", "field_name"):
            if not isinstance(act.get(required), str) or not act[required].strip():
                return jsonify({"success": False, "error": f"'{key}.{required}' must be a non-empty string"}), 400
        if act.get("field_name") != key:
            return jsonify({"success": False, "error": f"'{key}.field_name' must match the activity key"}), 400
        threshold = act.get("threshold")
        if threshold is not None:
            if not isinstance(threshold, dict):
                return jsonify({"success": False, "error": f"'{key}.threshold' must be a dict"}), 400
            mins = threshold.get("minutes")
            if not isinstance(mins, int) or mins < 1 or mins > 24 * 60:
                return jsonify({"success": False, "error": f"'{key}.threshold.minutes' must be an int in [1, 1440]"}), 400
        for list_field in ("calendar_keywords", "reset_triggers"):
            if list_field in act and not isinstance(act[list_field], list):
                return jsonify({"success": False, "error": f"'{key}.{list_field}' must be a list"}), 400

    target = _tracked_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                _json.dump(new_config, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(target))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.info(
            "[personalize] saved tracked-activities config (%d activities) to %s",
            len(activities), target,
        )
        return jsonify({"success": True, "config": new_config, "config_path": _TRACKED_REL_PATH})
    except Exception as e:
        logger.error("[personalize] activities write failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
