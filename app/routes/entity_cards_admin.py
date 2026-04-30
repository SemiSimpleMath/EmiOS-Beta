"""
/entity-cards-admin — dashboard for regenerating entity cards.

GET  /entity-cards-admin                 → dashboard HTML
GET  /entity-cards-admin/api/list        → JSON list (with filters)
GET  /entity-cards-admin/api/status      → current generation job state
POST /entity-cards-admin/api/regenerate/<card_id>  → regen single card (sync)
POST /entity-cards-admin/api/regenerate_all        → regen all/filtered (async)

Generation is single-slot system-wide: at most one card is being generated at
a time across every writer path (admin UI, /entity-card-maintenance regen,
nightly bulk runner). Serialization is enforced inside generate_and_persist_card
itself via the shared `card_gen_slot` context manager — see
`app/assistant/pipelines/entity_cards/card_gen_lock.py`. This module's
job is the UX: surface a 409 when the user clicks "regenerate" while a bulk
run is still in flight, instead of silently queuing requests.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from app.assistant.entity_management.entity_cards import EntityCard
from app.assistant.pipelines.entity_cards.card_gen_lock import CardGenSlotBusy
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

entity_cards_admin_bp = Blueprint("entity_cards_admin", __name__)
logger = get_logger(__name__)


# State for the bulk-regen UX ("is a bulk run currently in progress?").
# Per-card serialization lives in card_gen_lock.card_gen_slot — independent
# of this state. Together: the state lock prevents two bulk runs from
# starting at once; the card_gen_slot prevents per-card writes from
# overlapping (across this route, /entity-card-maintenance, the nightly
# bulk runner, and any future writer).
_GEN_STATE_LOCK = threading.Lock()
_GEN_STATE: Dict[str, Any] = {
    "running": False,
    "mode": None,
    "current_label": None,
    "current_node_id": None,
    "current_idx": 0,
    "total": 0,
    "started_at_utc": None,
    "finished_at_utc": None,
    "last_result": None,
    "errors": [],
}


_PLACEHOLDER_PATTERNS = [
    "No relationship evidence",
    "No contact information",
    "this batch provides no",
    "No information available",
    "No descriptive evidence",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_snapshot() -> Dict[str, Any]:
    with _GEN_STATE_LOCK:
        return dict(_GEN_STATE)


def _state_update(**fields: Any) -> None:
    with _GEN_STATE_LOCK:
        _GEN_STATE.update(fields)


def _state_reset_running(last_result: Optional[Dict[str, Any]] = None) -> None:
    with _GEN_STATE_LOCK:
        _GEN_STATE["running"] = False
        _GEN_STATE["current_label"] = None
        _GEN_STATE["current_node_id"] = None
        _GEN_STATE["finished_at_utc"] = _now_iso()
        if last_result is not None:
            _GEN_STATE["last_result"] = last_result


def _is_suspicious(card: EntityCard) -> bool:
    haystack = " ".join([card.summary or "", str(card.key_facts or "")])
    return any(pat in haystack for pat in _PLACEHOLDER_PATTERNS)


def _serialize_card(card: EntityCard) -> Dict[str, Any]:
    import json as _json

    def _as_list(raw: Any) -> List[Any]:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return []

    return {
        "id": card.id,
        "entity_name": card.entity_name,
        "entity_type": card.entity_type,
        "source_node_id": card.source_node_id,
        "summary": card.summary,
        "key_facts": _as_list(card.key_facts),
        "confidence": card.confidence,
        "updated_at": card.updated_at,
        "suspicious": _is_suspicious(card),
    }


@entity_cards_admin_bp.route("/entity-cards-admin", methods=["GET"])
def page() -> Any:
    return render_template("entity_cards_admin.html")


@entity_cards_admin_bp.route("/entity-cards-admin/api/list", methods=["GET"])
def api_list() -> Any:
    """Query params: q (search), type, suspicious_only (1/0), limit, offset."""
    q = (request.args.get("q") or "").strip()
    entity_type = (request.args.get("type") or "").strip()
    suspicious_only = request.args.get("suspicious_only") in ("1", "true", "yes")
    limit = request.args.get("limit", default=200, type=int)
    offset = request.args.get("offset", default=0, type=int)

    session = get_session()
    try:
        query = session.query(EntityCard).filter(EntityCard.is_active == True)  # noqa: E712
        if entity_type:
            query = query.filter(EntityCard.entity_type == entity_type)
        if q:
            like = f"%{q}%"
            query = query.filter(
                (EntityCard.entity_name.ilike(like))
                | (EntityCard.summary.ilike(like))
            )
        query = query.order_by(EntityCard.entity_name.asc())
        total_unfiltered = query.count()

        rows = query.offset(offset).limit(limit).all()
        serialized = [_serialize_card(r) for r in rows]
        if suspicious_only:
            serialized = [r for r in serialized if r["suspicious"]]

        suspicious_total = sum(1 for r in [_serialize_card(r) for r in query.all()] if r["suspicious"])

        return jsonify({
            "cards": serialized,
            "total": total_unfiltered,
            "suspicious_total": suspicious_total,
            "returned": len(serialized),
        })
    except Exception as e:
        logger.debug("[entity_cards_admin] api_list failed", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        session.close()


@entity_cards_admin_bp.route("/entity-cards-admin/api/status", methods=["GET"])
def api_status() -> Any:
    return jsonify(_state_snapshot())


@entity_cards_admin_bp.route(
    "/entity-cards-admin/api/regenerate/<card_id>", methods=["POST"]
)
def api_regenerate_single(card_id: str) -> Any:
    """Regenerate one card (kg_projection + summarizer).

    Single-slot enforced system-wide inside generate_and_persist_card. We
    pass ``blocking=False`` so the user sees a 409 immediately instead of
    waiting silently — the same UX as the bulk-in-progress check.
    """
    session = get_session()
    try:
        card = session.query(EntityCard).filter(EntityCard.id == card_id).first()
        if card is None:
            return jsonify({"error": "card_not_found"}), 404
        node_id = card.source_node_id
        label = card.entity_name
    finally:
        session.close()

    if not node_id:
        return jsonify({"error": "card_has_no_source_node_id"}), 400

    _state_update(
        running=True,
        mode="single",
        current_label=label,
        current_node_id=node_id,
        current_idx=1,
        total=1,
        started_at_utc=_now_iso(),
        finished_at_utc=None,
        last_result=None,
        errors=[],
    )

    from app.assistant.pipelines.entity_cards.card_writer import (
        generate_and_persist_card,
    )

    try:
        try:
            card_dict = generate_and_persist_card(label, force=True, blocking=False)
        except CardGenSlotBusy:
            _state_reset_running(last_result={
                "mode": "single", "label": label, "status": "busy",
            })
            return jsonify({
                "error": "generation_busy",
                "message": "Another card is being generated. Try again once it finishes.",
                "state": _state_snapshot(),
            }), 409

        # Strip the debug inputs blob before returning.
        card_dict_clean = {k: v for k, v in card_dict.items() if not k.startswith("_")}
        status = "created"
        result_payload = {"status": status, "label": label, "card_preview": card_dict_clean}
    except Exception as e:
        logger.error("[entity_cards_admin] regen failed for %s: %s", label, e)
        logger.debug("[entity_cards_admin] regen exception", exc_info=True)
        result_payload = {"status": "error", "label": label, "error": str(e)}
        status = "error"

    session = get_session()
    try:
        refreshed = (
            session.query(EntityCard).filter(EntityCard.id == card_id).first()
        )
        card_json = _serialize_card(refreshed) if refreshed else None
    finally:
        session.close()

    _state_reset_running(last_result={
        "mode": "single",
        "label": label,
        "status": status,
    })
    return jsonify({"result": result_payload, "card": card_json})


def _bulk_worker(card_ids: List[str]) -> None:
    """Serial worker — one card at a time via v2 writer.

    Each per-card call to ``generate_and_persist_card`` self-acquires the
    shared ``card_gen_slot``, so this worker doesn't need to hold any lock
    at the outer level. Other writer paths (admin single regen, maintenance
    regen, nightly bulk runner) interleave at per-card boundaries — each
    card write is atomic, no cross-talk possible.

    The ``_GEN_STATE['running']`` flag drives the bulk-in-progress UX 409;
    the worker clears it in ``_state_reset_running`` when done.
    """
    from app.assistant.pipelines.entity_cards.card_writer import generate_and_persist_card

    processed = 0
    errors: List[Dict[str, str]] = []

    try:
        for i, cid in enumerate(card_ids, start=1):
            session = get_session()
            try:
                card = session.query(EntityCard).filter(EntityCard.id == cid).first()
                if card is None or not card.source_node_id:
                    errors.append({"card_id": cid, "error": "not_found_or_no_node"})
                    continue
                node_id = card.source_node_id
                label = card.entity_name
            finally:
                session.close()

            _state_update(
                current_label=label,
                current_node_id=node_id,
                current_idx=i,
            )

            try:
                generate_and_persist_card(label, force=True)
                processed += 1
            except Exception as e:
                logger.error(
                    "[entity_cards_admin] bulk worker crashed on %s: %s", label, e
                )
                logger.debug("[entity_cards_admin] exception detail", exc_info=True)
                errors.append({"card_id": cid, "label": label, "error": str(e)})
    finally:
        _state_reset_running(last_result={
            "mode": "bulk",
            "total": len(card_ids),
            "processed": processed,
            "errors": len(errors),
            "error_detail": errors[:20],
        })


@entity_cards_admin_bp.route(
    "/entity-cards-admin/api/regenerate_all", methods=["POST"]
)
def api_regenerate_all() -> Any:
    """Body (all optional): { "card_ids": [...], "suspicious_only": true, "type": "..." }
    If card_ids is given, regenerate exactly those. Otherwise use filters.

    Refuses with 409 if a bulk run is already in progress (per
    ``_GEN_STATE['running']``). Single-card writers from other paths can
    still proceed concurrently — they serialize per-card via ``card_gen_slot``.
    """
    with _GEN_STATE_LOCK:
        if _GEN_STATE.get("running"):
            return jsonify({
                "error": "generation_busy",
                "state": _state_snapshot(),
            }), 409
        # Stake the bulk slot synchronously so concurrent requests see it.
        _GEN_STATE["running"] = True
        _GEN_STATE["mode"] = "bulk"

    payload = request.get_json(silent=True) or {}
    explicit_ids = payload.get("card_ids") if isinstance(payload.get("card_ids"), list) else None
    suspicious_only = bool(payload.get("suspicious_only"))
    entity_type = (payload.get("type") or "").strip()

    session = get_session()
    try:
        if explicit_ids:
            cards = (
                session.query(EntityCard)
                .filter(EntityCard.is_active == True)  # noqa: E712
                .filter(EntityCard.id.in_(explicit_ids))
                .all()
            )
        else:
            query = session.query(EntityCard).filter(EntityCard.is_active == True)  # noqa: E712
            if entity_type:
                query = query.filter(EntityCard.entity_type == entity_type)
            cards = query.all()
            if suspicious_only:
                cards = [c for c in cards if _is_suspicious(c)]
        card_ids = [c.id for c in cards]
    finally:
        session.close()

    if not card_ids:
        # Release the bulk slot we staked before the candidate query ran.
        _state_reset_running(last_result={"mode": "bulk", "status": "no_candidates"})
        return jsonify({"error": "no_candidates"}), 400

    _state_update(
        current_label=None,
        current_node_id=None,
        current_idx=0,
        total=len(card_ids),
        started_at_utc=_now_iso(),
        finished_at_utc=None,
        last_result=None,
        errors=[],
    )

    thread = threading.Thread(
        target=_bulk_worker,
        args=(card_ids,),
        name="entity-cards-bulk-regen",
        daemon=True,
    )
    thread.start()

    return jsonify({
        "status": "started",
        "total": len(card_ids),
        "state": _state_snapshot(),
    })
