"""Store-level helpers for synthetic-fact-proposal findings.

Reads + writes go through these functions instead of touching
kg_maintenance_finding.evidence_json directly so the review state
machine stays consistent.

State machine (lives in evidence_json.review_status):

    pending_review ──── user edits ────► edited
         │                                  │
         │                                  │ user submits
         ├── user rejects ──► rejected      │
         │                                  ▼
         └─ user approves directly ──► approved
                                          │
                                          │ extractor runs (TBD)
                                          ▼
                                    extracted (TBD)
                                          │
                                          │ promoted (TBD)
                                          ▼
                                    promoted (TBD)

Today the module ships pending_review / edited / approved / rejected
only — extracted/promoted come online when the wiki-aware extractor
and the merge-helper extraction land. The state machine is
forward-extensible without schema changes (it's all in evidence_json).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


FINDING_TYPE = "synthetic_fact_proposal"

REVIEW_STATUSES = (
    "pending_review",   # producer wrote it, user hasn't touched it
    "edited",           # user rewrote sentence / set dates / left notes
    "approved",         # user OK'd; awaiting structural extraction
    "rejected",         # user dismissed
    # planned future states once the extractor + merge helper land:
    # "extracted",      # fact_extractor produced a structural plan
    # "promoted",       # plan applied to KG via shared merge helper
)


def list_pending_review(*, limit: int = 100) -> List[Dict[str, Any]]:
    """Return pending_review and edited findings (the 'inbox' for
    the review UI). Sorted high-priority first, then newest-first."""
    from app.assistant.kg_maintenance.store import get_findings

    findings = get_findings(
        finding_type=FINDING_TYPE,
        status="pending",
        limit=limit,
    )
    out: List[Dict[str, Any]] = []
    for f in findings:
        ev = f.get("evidence_json") or {}
        if not isinstance(ev, dict):
            continue
        rs = ev.get("review_status", "pending_review")
        if rs in ("pending_review", "edited"):
            f["review_status"] = rs
            out.append(f)
    return out


def get_review_state(finding_id: str) -> Optional[Dict[str, Any]]:
    """Return the structured review state for one finding, or None
    when the finding doesn't exist / is the wrong type."""
    from app.assistant.kg_maintenance.store import get_finding

    f = get_finding(finding_id)
    if f is None or f.get("finding_type") != FINDING_TYPE:
        return None
    ev = f.get("evidence_json") or {}
    if not isinstance(ev, dict):
        ev = {}
    return {
        "finding_id": finding_id,
        "producer": ev.get("producer"),
        "subject_label": ev.get("subject_label"),
        "subject_node_id": f.get("primary_node_id"),
        "review_status": ev.get("review_status", "pending_review"),
        # The agent's original output (immutable).
        "agent_sentence": ev.get("sentence"),
        "agent_confidence": ev.get("agent_confidence"),
        "agent_suggested_start_date": ev.get("suggested_start_date"),
        "agent_suggested_end_date": ev.get("suggested_end_date"),
        "evidence_quote": ev.get("evidence_quote"),
        "inference_path": ev.get("inference_path"),
        # User edits (mutable — set by apply_user_edit).
        "user_edited_sentence": ev.get("user_edited_sentence"),
        "user_start_date": ev.get("user_start_date"),
        "user_end_date": ev.get("user_end_date"),
        "user_start_date_prose": ev.get("user_start_date_prose"),
        "user_end_date_prose": ev.get("user_end_date_prose"),
        "user_notes": ev.get("user_notes"),
        # Effective values (user > agent suggestion).
        "effective_sentence": ev.get("user_edited_sentence") or ev.get("sentence"),
        "effective_start_date": ev.get("user_start_date") or ev.get("suggested_start_date"),
        "effective_end_date": ev.get("user_end_date") or ev.get("suggested_end_date"),
    }


def apply_user_edit(
    finding_id: str,
    *,
    edited_sentence: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    start_date_prose: Optional[str] = None,
    end_date_prose: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist user edits onto the finding's evidence_json. Bumps
    review_status to 'edited' (or leaves at 'edited'/'approved' if it
    was already advanced — the edit just records the latest user input).

    Empty string clears a previously-set field; None means "leave
    alone." Returns the new review state dict.

    Caller is responsible for separately calling mark_approved() or
    mark_rejected() to advance the state machine. apply_user_edit is
    pure data persistence.
    """
    session = get_session()
    try:
        f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            raise ValueError(f"Finding {finding_id!r} not found")
        if f.finding_type != FINDING_TYPE:
            raise ValueError(
                f"Finding {finding_id} is type={f.finding_type!r}; "
                f"expected {FINDING_TYPE!r}"
            )
        ev = f.evidence_json or {}
        if not isinstance(ev, dict):
            ev = {}

        def _apply(key: str, value: Optional[str]):
            if value is None:
                return
            if value == "":
                ev.pop(key, None)
            else:
                ev[key] = value

        _apply("user_edited_sentence", edited_sentence)
        _apply("user_start_date", start_date)
        _apply("user_end_date", end_date)
        _apply("user_start_date_prose", start_date_prose)
        _apply("user_end_date_prose", end_date_prose)
        _apply("user_notes", notes)

        # Only bump pending_review → edited; preserve approved/rejected.
        cur = ev.get("review_status", "pending_review")
        if cur == "pending_review":
            ev["review_status"] = "edited"
        ev["last_edited_at"] = datetime.now(timezone.utc).isoformat()

        f.evidence_json = ev
        flag_modified(f, "evidence_json")
        session.commit()
    finally:
        session.close()

    return get_review_state(finding_id) or {}


def mark_approved(finding_id: str) -> Dict[str, Any]:
    """User approves the synthetic fact (with whatever edits they made).
    Bumps review_status to 'approved'. Does NOT yet trigger structural
    extraction or promotion — those are separate steps the wiki-aware
    extractor + shared merge helper will own once built."""
    return _set_review_status(finding_id, "approved")


def mark_rejected(finding_id: str) -> Dict[str, Any]:
    """User rejects. Bumps review_status to 'rejected' AND flips the
    underlying finding's status to 'rejected' (so it falls off the
    main maintenance dashboard's pending list)."""
    state = _set_review_status(finding_id, "rejected")
    # Also close the underlying finding so it's not still "pending."
    from app.assistant.kg_maintenance.store import set_status
    set_status(
        finding_id,
        "rejected",
        executed_by="ui:synthetic_fact_review",
        execution_notes="Rejected at synthetic-fact review.",
        cascade_to_siblings=False,  # synthetic-fact findings aren't clusters
    )
    return state


def _set_review_status(finding_id: str, new_status: str) -> Dict[str, Any]:
    if new_status not in REVIEW_STATUSES:
        raise ValueError(f"unknown review_status {new_status!r}")
    session = get_session()
    try:
        f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            raise ValueError(f"Finding {finding_id!r} not found")
        if f.finding_type != FINDING_TYPE:
            raise ValueError(
                f"Finding {finding_id} is type={f.finding_type!r}; "
                f"expected {FINDING_TYPE!r}"
            )
        ev = f.evidence_json or {}
        if not isinstance(ev, dict):
            ev = {}
        ev["review_status"] = new_status
        ev[f"{new_status}_at"] = datetime.now(timezone.utc).isoformat()
        f.evidence_json = ev
        flag_modified(f, "evidence_json")
        session.commit()
    finally:
        session.close()
    return get_review_state(finding_id) or {}
