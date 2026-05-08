"""
KGMaintenanceStore — single CRUD + query interface for kg_maintenance_finding.

Session lifecycle rule: every function opens its own short-lived session,
commits, and closes before returning.  No session is ever shared across calls.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def _priority_order_expr():
    """
    Build a CASE expression for priority ordering: high=0, medium=1, low=2.
    Constructed lazily (inside a query call) so SQLAlchemy mappers are fully
    configured before we reference mapped columns.
    """
    from sqlalchemy import case
    return case(
        (KGMaintenanceFinding.priority == "high", 0),
        (KGMaintenanceFinding.priority == "medium", 1),
        else_=2,
    )


# ── Write ─────────────────────────────────────────────────────────────────────

def upsert_finding(
    *,
    finding_type: str,
    primary_node_id: str,
    suggested_action: str,
    secondary_node_id: Optional[str] = None,
    edge_id: Optional[str] = None,
    reason: Optional[str] = None,
    confidence: Optional[float] = None,
    priority: str = "medium",
    agent_name: Optional[str] = None,
    evidence: Optional[dict] = None,
    pipeline_run_id: Optional[str] = None,
    initial_status: str = "pending",
) -> tuple[str, bool]:
    """
    Insert a new finding, or skip if a pending/approved one already exists for
    the same (finding_type, primary_node_id, secondary_node_id) triple.

    "Approved" findings count as open work (the user has flagged them for
    action but they haven't been resolved yet), so re-raising them as fresh
    pending rows would just flood the queue. Rejected and executed rows do
    NOT block re-raising — if the issue comes back, the scan should catch it.

    Normalises node pair ordering so (A,B) and (B,A) produce the same dedup key.
    Returns (finding_id, created) where created=False means it was a duplicate skip.
    """
    # Normalise: treat empty string same as None, then sort pair
    secondary_node_id = secondary_node_id or None
    if secondary_node_id and primary_node_id > secondary_node_id:
        primary_node_id, secondary_node_id = secondary_node_id, primary_node_id

    session = get_session()
    try:
        existing = (
            session.query(KGMaintenanceFinding.id)
            .filter(
                KGMaintenanceFinding.finding_type == finding_type,
                KGMaintenanceFinding.primary_node_id == primary_node_id,
                KGMaintenanceFinding.secondary_node_id == secondary_node_id,
                KGMaintenanceFinding.status.in_(("pending", "approved")),
            )
            .first()
        )

        if existing:
            return existing[0], False

        finding_id = str(uuid.uuid4())
        session.add(KGMaintenanceFinding(
            id=finding_id,
            finding_type=finding_type,
            status=initial_status,
            priority=priority,
            primary_node_id=primary_node_id,
            secondary_node_id=secondary_node_id,
            edge_id=edge_id,
            suggested_action=suggested_action,
            reason=reason,
            confidence=confidence,
            agent_name=agent_name,
            evidence_json=evidence,
            pipeline_run_id=pipeline_run_id,
        ))
        session.commit()
        logger.debug(
            "[KGMaintenanceStore] Created finding id=%s type=%s primary=%s",
            finding_id, finding_type, primary_node_id,
        )
        return finding_id, True

    except Exception:
        session.rollback()
        logger.debug("[KGMaintenanceStore] upsert_finding failed", exc_info=True)
        raise
    finally:
        session.close()


def set_status(
    finding_id: str,
    status: str,
    *,
    executed_by: Optional[str] = None,
    execution_notes: Optional[str] = None,
    cascade_to_siblings: bool = True,
) -> int:
    """Update a finding's status, optionally recording execution metadata.

    If the finding is a cluster lead (other findings have superseded_by ==
    this id) and ``cascade_to_siblings`` is True, every still-pending
    sibling gets the same status — resolving the lead resolves the whole
    cluster. Returns the number of siblings updated (0 if none / cascade
    disabled / not a lead).
    """
    session = get_session()
    try:
        finding = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if finding is None:
            raise ValueError(f"[KGMaintenanceStore] Finding {finding_id} not found")
        finding.status = status
        if executed_by:
            finding.executed_by = executed_by
            finding.executed_at = datetime.now(timezone.utc)
        if execution_notes:
            finding.execution_notes = execution_notes

        cascaded = 0
        if cascade_to_siblings:
            sibs = (
                session.query(KGMaintenanceFinding)
                .filter(KGMaintenanceFinding.superseded_by == finding_id)
                .filter(KGMaintenanceFinding.status == "pending")
                .all()
            )
            now = datetime.now(timezone.utc)
            for s in sibs:
                s.status = status
                s.executed_by = (executed_by or "cluster_cascade")
                s.executed_at = now
                s.execution_notes = (
                    f"Cascaded from cluster lead {finding_id} (status={status}). "
                    + (execution_notes or "")
                )[:500]
                cascaded += 1

        session.commit()
        return cascaded
    except Exception:
        session.rollback()
        logger.debug("[KGMaintenanceStore] set_status failed id=%s", finding_id, exc_info=True)
        raise
    finally:
        session.close()


# ── Execute ───────────────────────────────────────────────────────────────────

def execute_finding(finding_id: str) -> dict:
    """
    Execute the suggested_action for a finding by delegating to the
    step_execute_findings executor.

    Returns {"action": str, "executed": bool, "detail": str}.
    Raises ValueError if finding not found.
    """
    from app.assistant.pipelines.kg_maintenance_pipeline.step_execute_findings import (
        execute_single_finding,
    )

    finding = get_finding(finding_id)
    if finding is None:
        raise ValueError(f"Finding {finding_id!r} not found")

    try:
        result = execute_single_finding(finding)
    except Exception as exc:
        logger.error("[KGMaintenanceStore] execute_finding failed id=%s", finding_id)
        logger.debug("[KGMaintenanceStore] execute_finding exception", exc_info=True)
        set_status(
            finding_id, "execute_error",
            executed_by="ui",
            execution_notes=str(exc)[:500],
        )
        return {
            "action": finding.get("suggested_action", ""),
            "executed": False,
            "detail": f"Execution failed: {exc}",
        }

    if result.get("executed"):
        set_status(
            finding_id, "executed",
            executed_by="ui",
            execution_notes=result.get("detail", ""),
        )
    else:
        set_status(
            finding_id, "rejected",
            executed_by="ui",
            execution_notes=result.get("detail", ""),
        )

    return {
        "action": finding.get("suggested_action", ""),
        "executed": result.get("executed", False),
        "detail": result.get("detail", ""),
    }


# ── Read ──────────────────────────────────────────────────────────────────────

def get_findings(
    *,
    status: Optional[str] = "pending",
    finding_type: Optional[str] = None,
    priority: Optional[str] = None,
    exclude_types: Optional[list[str]] = None,
    include_superseded: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """
    Return findings as plain dicts sorted high → medium → low, then newest first.
    Enriches each finding with resolved node labels and edge counts.

    ``exclude_types`` removes findings of the listed finding_types from the
    result. Used by the main dashboard to hide background queues (date-gap
    questions, etc.) that have their own dedicated page.

    ``include_superseded=False`` (default) hides cluster siblings — findings
    whose ``superseded_by`` was stamped by the cluster_resolver agent. The
    surviving leads carry ``cluster_size`` = sibling_count + 1 so the UI
    can show "+N similar" badges. Set True to see every finding regardless
    of cluster membership (e.g. on a per-cluster expand view).
    """
    session = get_session()
    try:
        q = session.query(KGMaintenanceFinding)
        if status:
            q = q.filter(KGMaintenanceFinding.status == status)
        if finding_type:
            q = q.filter(KGMaintenanceFinding.finding_type == finding_type)
        if priority:
            q = q.filter(KGMaintenanceFinding.priority == priority)
        if exclude_types:
            q = q.filter(~KGMaintenanceFinding.finding_type.in_(exclude_types))
        if not include_superseded:
            q = q.filter(KGMaintenanceFinding.superseded_by.is_(None))
        q = q.order_by(_priority_order_expr(), KGMaintenanceFinding.created_at.desc())
        rows = q.limit(limit).offset(offset).all()
        findings = [_to_dict(r) for r in rows]
        _enrich_node_labels(findings, session)
        if not include_superseded:
            _stamp_cluster_size(findings, session)
        return findings
    except Exception:
        logger.debug("[KGMaintenanceStore] get_findings failed", exc_info=True)
        raise
    finally:
        session.close()


def _stamp_cluster_size(findings: list[dict], session) -> None:
    """For each finding that's a cluster lead, count its pending siblings
    and write ``cluster_size`` (= 1 + sibling count) onto the dict so the
    UI can render "+N similar" badges in one render pass.
    """
    from sqlalchemy import func as sqlfunc

    lead_ids = [f["id"] for f in findings if f.get("id")]
    if not lead_ids:
        return
    rows = (
        session.query(
            KGMaintenanceFinding.superseded_by,
            sqlfunc.count(KGMaintenanceFinding.id).label("cnt"),
        )
        .filter(KGMaintenanceFinding.superseded_by.in_(lead_ids))
        .filter(KGMaintenanceFinding.status == "pending")
        .group_by(KGMaintenanceFinding.superseded_by)
        .all()
    )
    sibling_count = {row[0]: int(row[1]) for row in rows}
    for f in findings:
        n = sibling_count.get(f.get("id"), 0)
        f["cluster_size"] = n + 1
        if n > 0 and isinstance(f.get("evidence_json"), dict):
            cluster = f["evidence_json"].get("cluster") or {}
            if isinstance(cluster, dict):
                f["cluster_root_question"] = cluster.get("root_question") or ""


def get_finding(finding_id: str) -> Optional[dict]:
    session = get_session()
    try:
        row = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        return _to_dict(row) if row else None
    except Exception:
        logger.debug("[KGMaintenanceStore] get_finding failed id=%s", finding_id, exc_info=True)
        raise
    finally:
        session.close()


def get_summary_counts(exclude_types: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Returns counts grouped by (finding_type, status) for the dashboard header.

    ``exclude_types`` removes the listed finding_types from ``total_pending``
    so the headline number matches the visible row count when the dashboard
    hides certain queues (e.g. ``state_missing_dates``, which lives on its
    own /date-gaps page). The full ``by_type`` breakdown is always returned
    so callers can render per-type badges (e.g. the "Date gaps (N) →" link).
    """
    from sqlalchemy import func as sqlfunc

    excluded = set(exclude_types or ())
    session = get_session()
    try:
        # Count only LEADS — findings that have been clustered (superseded_by
        # set) shouldn't inflate the headline number; the user resolves them
        # via the lead.
        rows = (
            session.query(
                KGMaintenanceFinding.finding_type,
                KGMaintenanceFinding.status,
                sqlfunc.count(KGMaintenanceFinding.id).label("cnt"),
            )
            .filter(KGMaintenanceFinding.superseded_by.is_(None))
            .group_by(KGMaintenanceFinding.finding_type, KGMaintenanceFinding.status)
            .all()
        )
        result: dict[str, Any] = {"by_type": {}, "total_pending": 0}
        for ftype, fstatus, cnt in rows:
            result["by_type"].setdefault(ftype, {})[fstatus] = cnt
            if fstatus == "pending" and ftype not in excluded:
                result["total_pending"] += cnt
        return result
    except Exception:
        logger.debug("[KGMaintenanceStore] get_summary_counts failed", exc_info=True)
        raise
    finally:
        session.close()


# ── Enrichment ────────────────────────────────────────────────────────────

def _enrich_node_labels(findings: list[dict], session) -> None:
    """Resolve primary/secondary node labels and edge counts in-place."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node
    from sqlalchemy import func as sqlfunc

    node_ids: set[str] = set()
    for f in findings:
        if f.get("primary_node_id"):
            node_ids.add(f["primary_node_id"])
        if f.get("secondary_node_id"):
            node_ids.add(f["secondary_node_id"])

    if not node_ids:
        return

    id_list = list(node_ids)
    label_map: dict[str, dict] = {}
    for batch_start in range(0, len(id_list), 500):
        batch = id_list[batch_start: batch_start + 500]
        rows = (
            session.query(Node.id, Node.label, Node.node_type, Node.category)
            .filter(Node.id.in_(batch))
            .all()
        )
        for r in rows:
            label_map[str(r.id)] = {
                "label": r.label or "",
                "node_type": r.node_type or "",
                "category": r.category or "",
            }

    outgoing = session.query(Edge.source_id, sqlfunc.count(Edge.id)).filter(
        Edge.source_id.in_(id_list)
    ).group_by(Edge.source_id).all()
    incoming = session.query(Edge.target_id, sqlfunc.count(Edge.id)).filter(
        Edge.target_id.in_(id_list)
    ).group_by(Edge.target_id).all()
    edge_counts: dict[str, int] = {}
    for nid, cnt in outgoing:
        edge_counts[str(nid)] = edge_counts.get(str(nid), 0) + cnt
    for nid, cnt in incoming:
        edge_counts[str(nid)] = edge_counts.get(str(nid), 0) + cnt

    for f in findings:
        pid = f.get("primary_node_id")
        sid = f.get("secondary_node_id")
        if pid and pid in label_map:
            f["primary_label"] = label_map[pid]["label"]
            f["primary_node_type"] = label_map[pid]["node_type"]
            f["primary_edge_count"] = edge_counts.get(pid, 0)
        if sid and sid in label_map:
            f["secondary_label"] = label_map[sid]["label"]
            f["secondary_node_type"] = label_map[sid]["node_type"]
            f["secondary_edge_count"] = edge_counts.get(sid, 0)


# ── Serialization ─────────────────────────────────────────────────────────────

def _to_dict(finding: KGMaintenanceFinding) -> dict:
    return {
        "id": finding.id,
        "finding_type": finding.finding_type,
        "status": finding.status,
        "priority": finding.priority,
        "primary_node_id": finding.primary_node_id,
        "secondary_node_id": finding.secondary_node_id,
        "edge_id": finding.edge_id,
        "suggested_action": finding.suggested_action,
        "reason": finding.reason,
        "confidence": finding.confidence,
        "agent_name": finding.agent_name,
        "evidence_json": finding.evidence_json,
        "investigation_report_json": finding.investigation_report_json,
        "investigated_at": finding.investigated_at.isoformat() if finding.investigated_at else None,
        "executed_by": finding.executed_by,
        "executed_at": finding.executed_at.isoformat() if finding.executed_at else None,
        "execution_notes": finding.execution_notes,
        "pipeline_run_id": finding.pipeline_run_id,
        "superseded_by": finding.superseded_by,
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
        "updated_at": finding.updated_at.isoformat() if finding.updated_at else None,
    }


# Public alias kept for any existing callers using `to_dict`
to_dict = _to_dict
