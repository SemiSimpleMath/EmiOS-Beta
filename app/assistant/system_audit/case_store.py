"""system_audit.case_store — the sole writer for system audit cases.

Session lifecycle rule: every function opens its own short-lived session,
commits, and closes before returning (kg_maintenance/store pattern). No
session is held over an LLM call.

Identity doctrine: dedup at open is an ID JOIN — a new signal sharing any
bound id with a non-terminal case ATTACHES to it (quotes/ids appended) rather
than minting a twin. Regression detection is a subsystem join at investigation
time. Wording similarity is never consulted.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.assistant.database.system_audit_case import SystemAuditCase
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import get_session

logger = get_logger(__name__)

_TERMINAL = {"resolved", "dismissed"}
ALLOWED_TRANSITIONS: Dict[str, set] = {
    "open": {"assembled", "dismissed"},
    "assembled": {"investigated", "dismissed"},
    "investigated": {"awaiting_claude", "resolved", "dismissed"},
    "awaiting_claude": {"resolved", "dismissed"},
    "regressed": {"awaiting_claude", "dismissed"},   # a regression goes straight to the inbox
    "resolved": set(),
    "dismissed": set(),
}


def _new_id() -> str:
    return f"sac_{uuid.uuid4().hex[:12]}"


def _ids_flat(bound_ids: Dict[str, Any]) -> set:
    out = set()
    for v in (bound_ids or {}).values():
        if isinstance(v, list):
            out.update(str(x) for x in v if x)
        elif v:
            out.add(str(v))
    return out


def open_case(*, trigger_kind: str, room_id: Optional[str], bound_ids: Dict[str, Any],
              summary: str, anchor_at=None, quote: Optional[Dict[str, Any]] = None) -> str:
    """Open a case — or ATTACH to a live one sharing any bound id. Returns the case id."""
    if trigger_kind not in ("user_friction", "auditor_finding"):
        raise ValueError(f"open_case: unknown trigger_kind {trigger_kind!r}")
    now = utc_now()
    anchor = anchor_at or now
    new_ids = _ids_flat(bound_ids)
    session = get_session()
    try:
        if new_ids:
            for row in session.query(SystemAuditCase).filter(
                    ~SystemAuditCase.status.in_(tuple(_TERMINAL))).all():
                if new_ids & _ids_flat(row.bound_ids):
                    merged = dict(row.bound_ids or {})
                    for k, v in (bound_ids or {}).items():
                        have = set(str(x) for x in merged.get(k, []))
                        add = [x for x in (v if isinstance(v, list) else [v])
                               if x and str(x) not in have]
                        merged[k] = list(merged.get(k, [])) + add
                    row.bound_ids = merged
                    if quote:
                        row.friction_quotes = list(row.friction_quotes or []) + [quote]
                    row.updated_at = now
                    session.commit()
                    logger.info("[case_store] attached signal to live case %s (id overlap)", row.id)
                    return row.id
        case = SystemAuditCase(
            id=_new_id(), opened_at=now, updated_at=now, trigger_kind=trigger_kind,
            status="open", room_id=room_id, bound_ids=bound_ids or {},
            friction_quotes=[quote] if quote else [], summary=summary, anchor_at=anchor,
        )
        session.add(case)
        session.commit()
        logger.info("[case_store] opened case %s (%s): %s", case.id, trigger_kind, summary[:80])
        return case.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def transition(case_id: str, target: str, **fields) -> None:
    """Move a case to `target`, updating any provided columns. Illegal moves raise."""
    session = get_session()
    try:
        case = session.get(SystemAuditCase, case_id)
        if case is None:
            raise KeyError(f"transition: case {case_id!r} not found")
        if target != case.status:
            allowed = ALLOWED_TRANSITIONS.get(case.status, set())
            if target not in allowed:
                raise ValueError(
                    f"illegal case transition {case.status!r}->{target!r} for {case_id}")
            case.status = target
        for k, v in fields.items():
            if not hasattr(case, k):
                raise ValueError(f"transition: unknown field {k!r}")
            setattr(case, k, v)
        case.updated_at = utc_now()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mark_investigated(case_id: str, *, preliminary_read: str, implicated_subsystem: str,
                      repair_suggestions: List[Dict[str, Any]], confidence: float) -> str:
    """Record the investigator's read. Returns the resulting status — 'regressed' when
    the implicated subsystem matches an already-RESOLVED case (the fix didn't hold),
    else 'investigated'."""
    session = get_session()
    try:
        prior = (session.query(SystemAuditCase)
                 .filter(SystemAuditCase.status == "resolved",
                         SystemAuditCase.implicated_subsystem == implicated_subsystem,
                         SystemAuditCase.id != case_id)
                 .order_by(SystemAuditCase.updated_at.desc())
                 .first())
        prior_id = prior.id if prior is not None else None
    finally:
        session.close()

    transition(case_id, "investigated", preliminary_read=preliminary_read,
               implicated_subsystem=implicated_subsystem,
               repair_suggestions=repair_suggestions, confidence=confidence)
    if prior_id:
        # Direct status write (regressed is a flag state outside the linear chain).
        session = get_session()
        try:
            case = session.get(SystemAuditCase, case_id)
            case.status = "regressed"
            case.recurrence_of = prior_id
            case.updated_at = utc_now()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        logger.warning("[case_store] case %s REGRESSED — subsystem %r was resolved in %s",
                       case_id, implicated_subsystem, prior_id)
        return "regressed"
    return "investigated"


def list_cases(*, statuses: Optional[List[str]] = None, limit: int = 50) -> List[Dict[str, Any]]:
    session = get_session()
    try:
        q = session.query(SystemAuditCase).order_by(SystemAuditCase.updated_at.desc())
        if statuses:
            q = q.filter(SystemAuditCase.status.in_(tuple(statuses)))
        return [{
            "id": c.id, "status": c.status, "trigger_kind": c.trigger_kind,
            "room_id": c.room_id, "summary": c.summary, "bound_ids": c.bound_ids,
            "friction_quotes": c.friction_quotes, "anchor_at": c.anchor_at,
            "dossier_path": c.dossier_path, "implicated_subsystem": c.implicated_subsystem,
            "recurrence_of": c.recurrence_of, "opened_at": c.opened_at,
            "preliminary_read": c.preliminary_read,
            "repair_suggestions": c.repair_suggestions,
            "resolution": c.resolution,
        } for c in q.limit(int(limit)).all()]
    finally:
        session.close()
