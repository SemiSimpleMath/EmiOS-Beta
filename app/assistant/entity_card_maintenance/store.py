"""
EntityCardMaintenanceStore — CRUD interface for entity_card_maintenance_finding.

Session lifecycle rule: every function opens its own short-lived session,
commits, and closes before returning.  No session is ever shared across calls.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.assistant.database.entity_card_maintenance_finding import EntityCardMaintenanceFinding
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def _priority_order_expr():
    from sqlalchemy import case
    return case(
        (EntityCardMaintenanceFinding.priority == "high",   0),
        (EntityCardMaintenanceFinding.priority == "medium", 1),
        else_=2,
    )


# ── Write ──────────────────────────────────────────────────────────────────────

def upsert_finding(
    *,
    finding_type: str,
    entity_card_id: str,
    suggested_action: str,
    reason: Optional[str] = None,
    confidence: Optional[float] = None,
    priority: str = "medium",
    evidence: Optional[dict] = None,
    pipeline_run_id: Optional[str] = None,
) -> tuple[str, bool]:
    """
    Insert a new finding, or skip if a pending finding already exists for the
    same (finding_type, entity_card_id) pair.

    Returns (finding_id, created) where created=False means it was skipped.
    """
    session = get_session()
    try:
        existing = (
            session.query(EntityCardMaintenanceFinding.id)
            .filter(
                EntityCardMaintenanceFinding.finding_type   == finding_type,
                EntityCardMaintenanceFinding.entity_card_id == entity_card_id,
                EntityCardMaintenanceFinding.status         == "pending",
            )
            .first()
        )

        if existing:
            return existing[0], False

        finding_id = str(uuid.uuid4())
        session.add(EntityCardMaintenanceFinding(
            id=finding_id,
            finding_type=finding_type,
            status="pending",
            priority=priority,
            entity_card_id=entity_card_id,
            suggested_action=suggested_action,
            reason=reason,
            confidence=confidence,
            evidence_json=evidence,
            pipeline_run_id=pipeline_run_id,
        ))
        session.commit()
        logger.debug(
            "[ECMaintenanceStore] Created finding id=%s type=%s card=%s",
            finding_id, finding_type, entity_card_id,
        )
        return finding_id, True

    except Exception:
        session.rollback()
        logger.debug("[ECMaintenanceStore] upsert_finding failed", exc_info=True)
        raise
    finally:
        session.close()


def set_status(
    finding_id: str,
    status: str,
    *,
    executed_by: Optional[str] = None,
    execution_notes: Optional[str] = None,
) -> None:
    """Update a finding's status, optionally recording execution metadata."""
    session = get_session()
    try:
        finding = session.query(EntityCardMaintenanceFinding).filter_by(id=finding_id).first()
        if finding is None:
            raise ValueError(f"[ECMaintenanceStore] Finding {finding_id!r} not found")
        finding.status = status
        if executed_by:
            finding.executed_by = executed_by
            finding.executed_at = datetime.now(timezone.utc)
        if execution_notes:
            finding.execution_notes = execution_notes
        session.commit()
    except Exception:
        session.rollback()
        logger.debug("[ECMaintenanceStore] set_status failed id=%s", finding_id, exc_info=True)
        raise
    finally:
        session.close()


def execute_approved_finding(finding_id: str, *, override_action: Optional[str] = None) -> dict:
    """
    Execute the suggested_action for an approved finding.

    Supported actions:
      - deactivate: sets EntityCard.is_active = False
      - regenerate: triggers v2 generate_and_persist_card for the entity
      - review: no-op (acknowledgement only)

    ``override_action`` lets the UI pick a different action than the default
    suggested_action — e.g. choosing "regenerate" on a blank_content finding
    whose default is "review".

    Returns {"action": str, "executed": bool, "detail": str}.
    """
    from app.assistant.entity_management.entity_cards import EntityCard

    finding = get_finding(finding_id)
    if finding is None:
        raise ValueError(f"Finding {finding_id!r} not found")

    action = (override_action or finding.get("suggested_action") or "").strip().lower()
    card_id = finding.get("entity_card_id")

    if action == "review":
        set_status(finding_id, "executed", executed_by="auto", execution_notes="Acknowledged (review only).")
        return {"action": action, "executed": True, "detail": "Acknowledged — no automated change needed."}

    if action == "regenerate":
        if not card_id:
            raise ValueError(f"Finding {finding_id!r} has no entity_card_id for regeneration.")
        session = get_session()
        try:
            card = session.query(EntityCard).filter_by(id=card_id).first()
            if card is None:
                set_status(finding_id, "executed", executed_by="auto", execution_notes="Card already deleted; nothing to regenerate.")
                return {"action": action, "executed": False, "detail": "Entity card not found — already deleted."}
            entity_name = card.entity_name
        finally:
            session.close()
        if not entity_name:
            raise ValueError(f"Finding {finding_id!r} target card {card_id} has no entity_name.")
        # v2 regenerate. Owns its own DB session; does its own LLM calls.
        from app.assistant.pipelines.entity_cards_v2.card_writer import generate_and_persist_card
        try:
            generate_and_persist_card(entity_name, force=True)
        except Exception as exc:
            logger.error("[ECMaintenanceStore] regenerate failed for %s: %s", entity_name, exc)
            logger.debug("[ECMaintenanceStore] regenerate exception", exc_info=True)
            set_status(
                finding_id, "approved",
                executed_by="auto",
                execution_notes=f"Regenerate failed: {exc!s}"[:500],
            )
            return {"action": action, "executed": False, "detail": f"Regenerate failed: {exc!s}"}
        set_status(finding_id, "executed", executed_by="auto", execution_notes=f"Regenerated card for '{entity_name}' via v2.")
        return {"action": action, "executed": True, "detail": f"Regenerated entity card '{entity_name}'."}

    if action == "deactivate":
        if not card_id:
            raise ValueError(f"Finding {finding_id!r} has no entity_card_id for deactivation.")
        session = get_session()
        try:
            card = session.query(EntityCard).filter_by(id=card_id).first()
            if card is None:
                set_status(finding_id, "executed", executed_by="auto", execution_notes="Card already deleted.")
                return {"action": action, "executed": True, "detail": "Entity card not found — already deleted."}
            if not card.is_active:
                set_status(finding_id, "executed", executed_by="auto", execution_notes="Card already inactive.")
                return {"action": action, "executed": True, "detail": "Entity card was already inactive."}
            card_name = card.entity_name or card_id
            card.is_active = False
            session.commit()
            logger.info("[ECMaintenanceStore] Deactivated entity card %s (%s) via finding %s", card_id, card_name, finding_id)
        except Exception:
            session.rollback()
            logger.debug("[ECMaintenanceStore] deactivate failed card=%s finding=%s", card_id, finding_id, exc_info=True)
            raise
        finally:
            session.close()
        set_status(finding_id, "executed", executed_by="auto", execution_notes=f"Deactivated card '{card_name}'.")
        return {"action": action, "executed": True, "detail": f"Deactivated entity card '{card_name}'."}

    set_status(finding_id, "executed", executed_by="auto", execution_notes=f"Unknown action '{action}' — marked executed.")
    logger.warning("[ECMaintenanceStore] Unknown suggested_action %r for finding %s", action, finding_id)
    return {"action": action, "executed": False, "detail": f"Unknown action '{action}' — no execution performed."}


# ── Read ───────────────────────────────────────────────────────────────────────

def get_findings(
    *,
    status: Optional[str] = "pending",
    finding_type: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Return findings as plain dicts sorted high → medium → low, then newest first."""
    session = get_session()
    try:
        q = session.query(EntityCardMaintenanceFinding)
        if status:
            q = q.filter(EntityCardMaintenanceFinding.status == status)
        if finding_type:
            q = q.filter(EntityCardMaintenanceFinding.finding_type == finding_type)
        if priority:
            q = q.filter(EntityCardMaintenanceFinding.priority == priority)
        q = q.order_by(_priority_order_expr(), EntityCardMaintenanceFinding.created_at.desc())
        rows = q.limit(limit).offset(offset).all()
        return [_to_dict(r) for r in rows]
    except Exception:
        logger.debug("[ECMaintenanceStore] get_findings failed", exc_info=True)
        raise
    finally:
        session.close()


def get_finding(finding_id: str) -> Optional[dict]:
    session = get_session()
    try:
        row = session.query(EntityCardMaintenanceFinding).filter_by(id=finding_id).first()
        return _to_dict(row) if row else None
    except Exception:
        logger.debug("[ECMaintenanceStore] get_finding failed id=%s", finding_id, exc_info=True)
        raise
    finally:
        session.close()


def get_summary_counts() -> dict[str, Any]:
    """Returns counts grouped by (finding_type, status) for the dashboard header."""
    from sqlalchemy import func as sqlfunc

    session = get_session()
    try:
        rows = (
            session.query(
                EntityCardMaintenanceFinding.finding_type,
                EntityCardMaintenanceFinding.status,
                sqlfunc.count(EntityCardMaintenanceFinding.id).label("cnt"),
            )
            .group_by(
                EntityCardMaintenanceFinding.finding_type,
                EntityCardMaintenanceFinding.status,
            )
            .all()
        )
        result: dict[str, Any] = {"by_type": {}, "total_pending": 0}
        for ftype, fstatus, cnt in rows:
            result["by_type"].setdefault(ftype, {})[fstatus] = cnt
            if fstatus == "pending":
                result["total_pending"] += cnt
        return result
    except Exception:
        logger.debug("[ECMaintenanceStore] get_summary_counts failed", exc_info=True)
        raise
    finally:
        session.close()


# ── Serialization ──────────────────────────────────────────────────────────────

def _to_dict(f: EntityCardMaintenanceFinding) -> dict:
    return {
        "id":               f.id,
        "finding_type":     f.finding_type,
        "status":           f.status,
        "priority":         f.priority,
        "entity_card_id":   f.entity_card_id,
        "suggested_action": f.suggested_action,
        "reason":           f.reason,
        "confidence":       f.confidence,
        "evidence_json":    f.evidence_json,
        "executed_by":      f.executed_by,
        "executed_at":      f.executed_at.isoformat() if f.executed_at else None,
        "execution_notes":  f.execution_notes,
        "pipeline_run_id":  f.pipeline_run_id,
        "created_at":       f.created_at.isoformat() if f.created_at else None,
        "updated_at":       f.updated_at.isoformat() if f.updated_at else None,
    }
