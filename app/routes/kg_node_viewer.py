"""
KG Node Viewer

Read-only node detail page (edit handler added in a follow-up).

Route: GET /kg/node/<node_id>

Shows:
  - Core node fields
  - In-edges and out-edges (clickable to navigate)
  - Evidence: for each source window, the window items that produced the node
"""
from __future__ import annotations

import json
from flask import Blueprint, abort, render_template

kg_node_viewer_bp = Blueprint("kg_node_viewer", __name__)


def _load_node(session, node_id: str) -> dict | None:
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge

    node = session.query(Node).filter(Node.id == node_id).first()
    if node is None:
        return None

    def _iso(x):
        if x is None:
            return None
        try:
            return x.isoformat()
        except Exception:
            return str(x)

    out_edges = (
        session.query(Edge)
        .filter(Edge.source_id == node_id)
        .limit(100)
        .all()
    )
    in_edges = (
        session.query(Edge)
        .filter(Edge.target_id == node_id)
        .limit(100)
        .all()
    )

    def _other_node(nid: str):
        if not nid:
            return None
        other = session.query(Node).filter(Node.id == nid).first()
        if not other:
            return {"id": nid, "label": "(missing)", "node_type": ""}
        return {"id": str(other.id), "label": other.label, "node_type": other.node_type}

    out_list = [
        {
            "id": str(e.id),
            "relationship_type": e.relationship_type,
            "relationship_descriptor": e.relationship_descriptor,
            "sentence": e.sentence,
            "other": _other_node(e.target_id),
        }
        for e in out_edges
    ]
    in_list = [
        {
            "id": str(e.id),
            "relationship_type": e.relationship_type,
            "relationship_descriptor": e.relationship_descriptor,
            "sentence": e.sentence,
            "other": _other_node(e.source_id),
        }
        for e in in_edges
    ]

    return {
        "id": str(node.id),
        "label": node.label,
        "node_type": node.node_type,
        "category": node.category,
        "description": node.description,
        "original_sentence": node.original_sentence,
        "aliases": node.aliases or [],
        "hash_tags": node.hash_tags or [],
        "semantic_label": node.semantic_label,
        "goal_status": node.goal_status,
        "valid_during": node.valid_during,
        "start_date": _iso(node.start_date),
        "end_date": _iso(node.end_date),
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
        "start_date_prose": getattr(node, "start_date_prose", None),
        "end_date_prose": getattr(node, "end_date_prose", None),
        "confidence": node.confidence,
        "importance": node.importance,
        "source": node.source,
        "created_at": _iso(node.created_at),
        "updated_at": _iso(node.updated_at),
        # Per-observation provenance (window_id, source message, etc.) is
        # rendered separately by _load_provenance() reading kg_node_evidence.
        "in_edges": in_list,
        "out_edges": out_list,
    }


def _load_provenance(session, node_id: str) -> list[dict]:
    """For each window that contributed evidence for this node, return window items.

    Walks the unified pipeline tables: ``kg_window_message`` for membership,
    ``unified_log_2026`` for verbatim text + speaker, ``kg_resolved_message``
    for entity-resolved text when available.
    """
    from sqlalchemy import text

    rows = session.execute(
        text("""
            SELECT DISTINCT window_id, MIN(created_at) AS first_seen
            FROM kg_node_evidence
            WHERE node_id = :nid
            GROUP BY window_id
            ORDER BY first_seen DESC
        """),
        {"nid": node_id},
    ).fetchall()

    prov = []
    for r in rows:
        wid = r[0]
        if not wid:
            continue
        items = session.execute(
            text("""
                SELECT
                    sm.item_order,
                    ul.role,
                    ul.speaker_name,
                    COALESCE(rm.resolved_text, ul.message) AS text,
                    ul.timestamp                          AS unified_timestamp
                FROM kg_window_message sm
                JOIN unified_log_2026 ul        ON ul.id = sm.unified_log_id
                LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = sm.unified_log_id
                WHERE sm.window_id = :wid
                ORDER BY sm.item_order
            """),
            {"wid": wid},
        ).fetchall()
        evidence_sentences = session.execute(
            text("""
                SELECT derived_sentence, merge_action, created_at
                FROM kg_node_evidence
                WHERE node_id = :nid AND window_id = :wid
                ORDER BY created_at
            """),
            {"nid": node_id, "wid": wid},
        ).fetchall()
        prov.append({
            "window_id": wid,
            "window_items": [
                {"order": it[0], "role": it[1], "speaker": it[2], "text": it[3], "ts": str(it[4]) if it[4] else None}
                for it in items
            ],
            "evidence": [
                {"sentence": ev[0], "action": ev[1], "ts": str(ev[2]) if ev[2] else None}
                for ev in evidence_sentences
            ],
        })
    return prov


@kg_node_viewer_bp.route("/kg/node/<node_id>")
def kg_node_view(node_id: str):
    from app.models.base import get_session

    session = get_session()
    try:
        node = _load_node(session, node_id)
        if node is None:
            abort(404, f"Node {node_id} not found")
        provenance = _load_provenance(session, node_id)
        proposals = _load_proposals_for_node(session, node_id)
    finally:
        session.close()

    return render_template(
        "kg_node_viewer.html",
        node=node,
        provenance=provenance,
        provenance_json=json.dumps(provenance, default=str),
        proposals=proposals,
    )


def _load_proposals_for_node(session, node_id: str) -> list[dict]:
    """Return all claim proposals that reference this KG node (via
    resolved_node_id on any claim_proposal_node row). Many-to-many: a
    single KG entity like Jukka will appear in many proposals."""
    try:
        from app.assistant.database.claim_proposals import (
            ClaimProposal, ClaimProposalNode,
        )
    except Exception:
        return []
    rows = (
        session.query(ClaimProposal, ClaimProposalNode)
        .join(ClaimProposalNode, ClaimProposalNode.proposal_id == ClaimProposal.id)
        .filter(ClaimProposalNode.resolved_node_id == node_id)
        .order_by(ClaimProposal.created_at.desc())
        .limit(100)
        .all()
    )
    out = []
    for p, cpn in rows:
        out.append({
            "id": p.id,
            "short_id": p.id[:8],
            "status": p.status,
            "claim_type": p.claim_type,
            "representative_sentence": p.representative_sentence,
            "role_in_proposal": cpn.node_type,
            "resolution_action": cpn.resolution_action,
            "created_at": str(p.created_at)[:19] if p.created_at else None,
        })
    return out
