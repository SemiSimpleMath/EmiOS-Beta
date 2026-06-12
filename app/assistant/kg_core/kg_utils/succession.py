"""Diachronic succession split for role-reference entities.

A role-reference entity ("X's School", "X's Car") names a role whose
concrete referent changes over time. When evidence shows the referent
changed, the node must SPLIT into eras instead of blending them:

    old node      — keeps its label, era closed (end_date = split date)
    successor     — same label, new node, era opens at the split date
    Disambiguation— minted at the label so future mentions PARK there
                    (the promoter's existing precedence rule), drained
                    by date via the disambiguation scan
    succeeded_by  — edge old → successor, making the chain queryable

Splits are evidence-driven and lazy — nothing here decides WHETHER to
split (the succession judge + human review do); this module only
executes a decided split atomically in the caller's session.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

VALID_DATE_CONFIDENCES = ("user_set", "actual", "estimated", "inferred")


def _node_snapshot(node) -> Dict[str, Any]:
    return {
        "id": node.id,
        "label": node.label,
        "node_type": node.node_type,
        "start_date": node.start_date.isoformat() if node.start_date else None,
        "end_date": node.end_date.isoformat() if node.end_date else None,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
    }


def split_succession(
    session,
    *,
    node_id: str,
    split_date: datetime,
    reason: str,
    date_confidence: str = "estimated",
    repoint_edge_ids: Optional[List[str]] = None,
    finding_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a decided era split in the caller's session (caller commits).

    Raises ValueError on any refusal — nothing is mutated in that case
    (all validation happens before the first write).

    Returns {old_node_id, successor_node_id, disambiguation_node_id,
    succeeded_by_edge_id, repointed, skipped_repoints, revision_log_id}.
    The successor is NOT chroma-embedded here — callers do that after
    their commit (chroma is not transactional with sqlite).
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from app.assistant.kg.disambiguation import (
        DISAMBIGUATION_NODE_TYPE,
        create_disambiguation,
    )

    if not (reason or "").strip():
        raise ValueError("`reason` is required.")
    if date_confidence not in VALID_DATE_CONFIDENCES:
        raise ValueError(f"date_confidence must be one of {VALID_DATE_CONFIDENCES}.")

    old = session.get(Node, node_id)
    if old is None:
        raise ValueError(f"node {node_id!r} not found")
    if old.node_type == DISAMBIGUATION_NODE_TYPE:
        raise ValueError("cannot split a Disambiguation node")
    if old.node_type != "Entity":
        raise ValueError(
            f"split_succession is for role-reference Entities; {old.label!r} is "
            f"{old.node_type} (States/Events close via kg_close_state instead)."
        )
    if getattr(old, "locked_by_user_at", None) is not None:
        raise ValueError(f"node {old.label!r} is user-locked; unlock before splitting.")
    if old.end_date is not None:
        raise ValueError(
            f"node {old.label!r} era is already closed (end_date set) — "
            f"its successor should already exist."
        )
    old_start = old.start_date
    if old_start is not None:
        from app.assistant.kg_core.kg_utils.date_compare import as_aware_utc
        if as_aware_utc(split_date) <= as_aware_utc(old_start):
            raise ValueError(
                f"split_date {split_date.date()} must postdate the node's "
                f"start_date {old_start.date()}."
            )

    # Validate repoints up front (all-or-nothing per edge happens later,
    # but missing edges are a caller error, not a skip).
    repoint_edges = []
    for eid in (repoint_edge_ids or []):
        e = session.get(Edge, eid)
        if e is None:
            raise ValueError(f"repoint edge {eid!r} not found")
        if old.id not in (e.source_id, e.target_id):
            raise ValueError(f"edge {eid!r} does not touch node {old.label!r}")
        repoint_edges.append(e)

    before = _node_snapshot(old)

    # 1. Close the old era.
    old.end_date = split_date
    old.end_date_confidence = date_confidence

    # 2. Mint the successor — same label, era opens at the split.
    successor = Node(
        id=str(uuid.uuid4()),
        label=old.label,
        node_type="Entity",
        category=old.category,
        start_date=split_date,
        start_date_confidence=date_confidence,
        original_sentence=(
            f"The current {old.label} (since {split_date.date().isoformat()}); "
            f"succeeded the earlier {old.label} era."
        ),
        attributes={},
    )
    session.add(successor)
    session.flush()

    # 3. Park point for future mentions at this now-ambiguous label.
    dis = create_disambiguation(
        session,
        label=old.label,
        reason=(
            f"Successive referents: the label has era-distinct nodes "
            f"(split {split_date.date().isoformat()}). New mentions park here; "
            f"the disambiguation scan re-points them by evidence date."
        ),
    )

    # 4. Queryable succession chain.
    succ_edge = Edge(
        id=str(uuid.uuid4()),
        source_id=old.id,
        target_id=successor.id,
        relationship_type="succeeded_by",
        sentence=(
            f"{old.label} (era ending {split_date.date().isoformat()}) was "
            f"succeeded by the current {old.label}."
        ),
        attributes={},
    )
    session.add(succ_edge)
    session.flush()

    # 5. Optional repoints of era-mismatched edges onto the successor.
    repointed: List[str] = []
    skipped: List[Dict[str, str]] = []
    for e in repoint_edges:
        if getattr(e, "locked_by_user_at", None) is not None:
            skipped.append({"edge_id": e.id, "why": "user-locked"})
            continue
        new_src = successor.id if e.source_id == old.id else e.source_id
        new_tgt = successor.id if e.target_id == old.id else e.target_id
        dup = (
            session.query(Edge)
            .filter(Edge.source_id == new_src, Edge.target_id == new_tgt,
                    Edge.relationship_type == e.relationship_type,
                    Edge.id != e.id)
            .first()
        )
        if dup is not None:
            skipped.append({"edge_id": e.id, "why": f"duplicate of {dup.id[:8]}"})
            continue
        e.source_id, e.target_id = new_src, new_tgt
        repointed.append(e.id)
    session.flush()

    rid = str(uuid.uuid4())
    session.add(KGRevisionLog(
        id=rid,
        op="split_succession",
        args_json={
            "node_id": node_id,
            "split_date": split_date.isoformat(),
            "date_confidence": date_confidence,
            "repoint_edge_ids": list(repoint_edge_ids or []),
        },
        before_json=before,
        after_json={
            "old": _node_snapshot(old),
            "successor": _node_snapshot(successor),
            "disambiguation_node_id": dis.id,
            "succeeded_by_edge_id": succ_edge.id,
            "repointed": repointed,
            "skipped_repoints": skipped,
        },
        reason=reason,
        finding_id=finding_id,
        agent_id=agent_id,
        succeeded=1,
    ))

    logger.info(
        "[succession] split %r: era closed %s, successor %s, %d edge(s) repointed",
        old.label, split_date.date().isoformat(), successor.id[:8], len(repointed),
    )
    return {
        "old_node_id": old.id,
        "successor_node_id": successor.id,
        "successor_label": successor.label,
        "successor_sentence": successor.original_sentence,
        "disambiguation_node_id": dis.id,
        "succeeded_by_edge_id": succ_edge.id,
        "repointed": repointed,
        "skipped_repoints": skipped,
        "revision_log_id": rid,
    }
