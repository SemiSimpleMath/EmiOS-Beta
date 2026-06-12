"""
Step: disambiguation_scan

Disambiguation nodes are attachment points: mentions whose true referent
is unknown bind to them, and their edges accumulate until they are
re-pointed to the right node.

Two-phase drain:

1. TEMPORAL DRAIN (deterministic, closed-form): when the label has 2+
   same-label era nodes (a diachronic succession split), each parked
   edge whose evidence date falls inside EXACTLY ONE era's
   [start_date, end_date) window is re-pointed to that era — no LLM.
   Anything else (undated evidence, overlapping/undated eras, locks,
   would-be duplicates) stays parked: a deterministic miss must sink,
   not drop.

2. BACKLOG FINDING: every Disambiguation node still carrying edges gets
   a "disambiguation_backlog" finding for the investigator, who
   determines referents from relationship facts (kg_repoint_edge).

Edge-less Disambiguation nodes are the resting state and raise nothing.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import func, or_

from app.assistant.kg_core.kg_utils.date_compare import as_aware_utc, in_window
from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def _era_contains(node, d: datetime) -> bool:
    return in_window(d, node.start_date, node.end_date)


def temporal_drain(session, dis_node_id: str, label: str) -> Dict[str, Any]:
    """Re-point parked edges by evidence date — closed-form cases only.

    Mutates in the caller's session (caller commits). Returns
    {"repointed": [edge_ids], "left_parked": int}.
    """
    from app.assistant.database.kg_chat_projection import KGEdgeEvidence
    from app.assistant.database.kg_revision_log import KGRevisionLog
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from app.assistant.kg.disambiguation import DISAMBIGUATION_NODE_TYPE

    eras = (
        session.query(Node)
        .filter(func.lower(Node.label) == (label or "").lower())
        .filter(Node.node_type != DISAMBIGUATION_NODE_TYPE)
        .all()
    )
    parked = (
        session.query(Edge)
        .filter(or_(Edge.source_id == dis_node_id, Edge.target_id == dis_node_id))
        .all()
    )
    # The date rule only applies to succession-split labels: 2+ same-label
    # era nodes. Synchronic twins (two Alexes) are typically undated and
    # correctly fall through to the investigator.
    if len(eras) < 2:
        return {"repointed": [], "left_parked": len(parked)}

    repointed: List[str] = []
    for e in parked:
        if getattr(e, "locked_by_user_at", None) is not None:
            continue
        ev = (
            session.query(KGEdgeEvidence)
            .filter(KGEdgeEvidence.edge_id == e.id)
            .order_by(KGEdgeEvidence.message_timestamp.asc().nulls_last())
            .first()
        )
        d = as_aware_utc(ev.message_timestamp) if ev is not None and ev.message_timestamp else None
        d = d or as_aware_utc(e.created_at)
        if d is None:
            continue
        containing = [era for era in eras if _era_contains(era, d)]
        if len(containing) != 1:
            continue  # ambiguous or no era — sink to the investigator
        era = containing[0]
        if getattr(era, "locked_by_user_at", None) is not None:
            continue
        new_src = era.id if e.source_id == dis_node_id else e.source_id
        new_tgt = era.id if e.target_id == dis_node_id else e.target_id
        if new_src == new_tgt:
            continue
        dup = (
            session.query(Edge)
            .filter(Edge.source_id == new_src, Edge.target_id == new_tgt,
                    Edge.relationship_type == e.relationship_type,
                    Edge.id != e.id)
            .first()
        )
        if dup is not None:
            continue
        before = {"source_id": e.source_id, "target_id": e.target_id,
                  "relationship_type": e.relationship_type}
        e.source_id, e.target_id = new_src, new_tgt
        session.add(KGRevisionLog(
            id=str(uuid.uuid4()),
            op="repoint_edge",
            args_json={"edge_id": e.id, "new_source_id": new_src,
                       "new_target_id": new_tgt, "finding_id": None},
            before_json=before,
            after_json={**before, "source_id": new_src, "target_id": new_tgt},
            reason=(
                f"Temporal drain: evidence dated {d.date().isoformat()} falls "
                f"inside exactly one {label!r} era "
                f"({era.start_date.date().isoformat() if era.start_date else 'open'}"
                f" → {era.end_date.date().isoformat() if era.end_date else 'open'})."
            ),
            finding_id=None,
            agent_id="disambiguation_scan_temporal_drain",
            succeeded=1,
        ))
        repointed.append(e.id)

    session.flush()
    return {"repointed": repointed, "left_parked": len(parked) - len(repointed)}


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "drained": int, "with_backlog": int,
    "new_findings": int}."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node
    from app.assistant.kg.disambiguation import DISAMBIGUATION_NODE_TYPE
    from app.models.db_manager import get_db_manager

    # Read phase — short-lived session
    session = get_session()
    try:
        dis_rows = (
            session.query(Node.id, Node.label)
            .filter(Node.node_type == DISAMBIGUATION_NODE_TYPE)
            .all()
        )
        dis_list = [(str(r.id), r.label or "") for r in dis_rows]
    finally:
        session.close()

    # Phase 1: deterministic temporal drain (own write transaction per node).
    drained = 0
    for node_id, label in dis_list:
        try:
            with get_db_manager().transaction(op="disambiguation_temporal_drain") as s:
                result = temporal_drain(s, node_id, label)
            if result["repointed"]:
                drained += len(result["repointed"])
                logger.info(
                    "[disambiguation_scan] temporal drain re-pointed %d edge(s) "
                    "parked at %r", len(result["repointed"]), label,
                )
        except Exception:
            logger.error(
                "[disambiguation_scan] temporal drain failed for %r", label,
                exc_info=True,
            )

    # Phase 2: anything still parked raises a backlog finding.
    session = get_session()
    try:
        backlog: list[tuple[str, str, int]] = []
        for node_id, label in dis_list:
            edge_count = (
                session.query(Edge.id)
                .filter(or_(Edge.source_id == node_id, Edge.target_id == node_id))
                .count()
            )
            if edge_count > 0:
                backlog.append((node_id, label, edge_count))
    finally:
        session.close()

    new_findings = 0
    for node_id, label, edge_count in backlog:
        _, created = upsert_finding(
            finding_type="disambiguation_backlog",
            primary_node_id=node_id,
            suggested_action="repoint_edges",
            reason=(
                f"Disambiguation node '{label}' has accumulated {edge_count} "
                f"edge(s) from mentions whose referent was unknown at write "
                f"time. Determine each edge's true referent and re-point it."
            ),
            confidence=1.0,
            priority="medium",
            agent_name="disambiguation_scan",
            evidence={"label": label, "edge_count": edge_count},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[disambiguation_scan] %d Disambiguation nodes scanned, %d edges "
        "temporally drained, %d with backlog, %d new findings",
        len(dis_list), drained, len(backlog), new_findings,
    )
    return {
        "scanned": len(dis_list),
        "drained": drained,
        "with_backlog": len(backlog),
        "new_findings": new_findings,
    }
