"""Disambiguation nodes — learned corrections in the KG itself.

A Disambiguation node sits at a label that the system has had trouble
classifying (e.g., "Friday Night Meats" — first time the extractor saw
it, was it a recurring-concept Entity or a per-occurrence Event?). The
node serves three purposes:

  1. **Authority** — `disambiguates_to` edge points at the canonical
     Node for that label. The resolver consults this BEFORE deciding
     where a fresh proposal lands.

  2. **Memory** — the Disambiguation captures the type-mistake that was
     previously caught (e.g., "previously misclassified as Event").
     Future readers see what was learned without re-deriving.

  3. **Inbox** — when the resolver gets a new proposal at the same
     label but cannot cleanly merge (type mismatch, ambiguous identity),
     it `pending_resolution` parks the new Node on this Disambiguation.
     A later maintenance sweep walks parked nodes and routes through
     the investigator.

Disambiguation nodes are first-class KG nodes (node_type =
'Disambiguation'), so they're queryable, audit-friendly, and live where
the rest of the graph does. They have no temporal anchor of their own —
they're meta-graph correction records, not life-graph claims.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func, or_
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

DISAMBIGUATION_NODE_TYPE = "Disambiguation"
DISAMBIGUATES_TO_REL = "disambiguates_to"
PENDING_RESOLUTION_REL = "pending_resolution"


# ── Lookup ────────────────────────────────────────────────────────────────

def find_disambiguation(session: Any, label: str) -> Optional[Any]:
    """Return the Disambiguation node at this label, or None.

    Case-insensitive label match. If multiple match (shouldn't happen
    in practice), returns the first by creation order so behavior is
    deterministic.
    """
    from app.assistant.kg.db.knowledge_graph_db import Node

    if not label:
        return None
    label_norm = label.lower().strip()
    if not label_norm:
        return None
    return (
        session.query(Node)
        .filter(func.lower(Node.label) == label_norm)
        .filter(Node.node_type == DISAMBIGUATION_NODE_TYPE)
        .order_by(Node.created_at.asc())
        .first()
    )


def resolve_through_disambiguation(session: Any, label: str) -> Optional[Any]:
    """Follow `disambiguates_to` from a Disambiguation at this label to
    its canonical Node. Returns None if no Disambiguation exists or if
    the disambiguation has no `disambiguates_to` edge.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    dis = find_disambiguation(session, label)
    if dis is None:
        return None
    edge = (
        session.query(Edge)
        .filter(Edge.source_id == dis.id)
        .filter(Edge.relationship_type == DISAMBIGUATES_TO_REL)
        .first()
    )
    if edge is None:
        return None
    return session.query(Node).filter_by(id=edge.target_id).first()


# ── Mint ──────────────────────────────────────────────────────────────────

def create_disambiguation(
    session: Any,
    *,
    label: str,
    canonical_node_id: str,
    previously_misclassified_as: Optional[str] = None,
    reason: str = "",
) -> Any:
    """Mint a Disambiguation node at `label` and link it to the canonical
    node via `disambiguates_to`.

    Returns the new Node (already added to the session; caller commits).

    Idempotent: if a Disambiguation already exists at this label, the
    existing one is returned without re-creating.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    existing = find_disambiguation(session, label)
    if existing is not None:
        return existing

    note_bits = [
        f"Disambiguation marker for label {label!r}.",
        f"Canonical node id: {canonical_node_id}.",
    ]
    if previously_misclassified_as:
        note_bits.append(
            f"Previously misclassified as {previously_misclassified_as}."
        )
    if reason:
        note_bits.append(reason.strip())
    original_sentence = " ".join(note_bits)

    dis = Node(
        id=str(uuid.uuid4()),
        label=label,
        node_type=DISAMBIGUATION_NODE_TYPE,
        original_sentence=original_sentence,
        description=(
            f"Marker that resolves label {label!r} to a canonical Node "
            f"and parks ambiguous new proposals via "
            f"`{PENDING_RESOLUTION_REL}` edges for later review."
        ),
    )
    session.add(dis)
    session.flush()

    edge = Edge(
        id=str(uuid.uuid4()),
        source_id=dis.id,
        target_id=canonical_node_id,
        relationship_type=DISAMBIGUATES_TO_REL,
        sentence=(
            f"Label {label!r} resolves to canonical node "
            f"{canonical_node_id}."
        ),
    )
    session.add(edge)
    session.flush()

    logger.info(
        "[disambiguation] minted node %s at label %r -> canonical %s",
        dis.id[:8], label, canonical_node_id[:8],
    )
    return dis


# ── Park ──────────────────────────────────────────────────────────────────

def park_node_on_disambiguation(
    session: Any,
    *,
    new_node_id: str,
    disambiguation_node_id: str,
    reason: str = "",
) -> None:
    """Add a `pending_resolution` edge from new_node → disambiguation.

    Idempotent: existing edge is skipped.

    Used when the resolver can't cleanly merge a fresh proposal into the
    canonical (type mismatch, ambiguous identity) but a Disambiguation
    at the same label tells us this label has a canonical answer. The
    new node still gets created so no data is lost; the parking edge
    flags it for the maintenance sweep's investigator to route.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge

    existing = (
        session.query(Edge)
        .filter(Edge.source_id == new_node_id)
        .filter(Edge.target_id == disambiguation_node_id)
        .filter(Edge.relationship_type == PENDING_RESOLUTION_REL)
        .first()
    )
    if existing is not None:
        return

    edge = Edge(
        id=str(uuid.uuid4()),
        source_id=new_node_id,
        target_id=disambiguation_node_id,
        relationship_type=PENDING_RESOLUTION_REL,
        sentence=(
            f"Parked on disambiguation for later review. "
            + (reason if reason else "Resolver could not merge cleanly.")
        )[:500],
    )
    session.add(edge)
    session.flush()
    logger.info(
        "[disambiguation] parked node %s on disambiguation %s",
        new_node_id[:8], disambiguation_node_id[:8],
    )


# ── List parked nodes (for maintenance sweep) ────────────────────────────

def list_parked_nodes(session: Any, disambiguation_node_id: str) -> list:
    """All nodes parked on the given Disambiguation, oldest first."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    rows = (
        session.query(Node, Edge)
        .join(Edge, Edge.source_id == Node.id)
        .filter(Edge.target_id == disambiguation_node_id)
        .filter(Edge.relationship_type == PENDING_RESOLUTION_REL)
        .order_by(Edge.created_at.asc().nullslast())
        .all()
    )
    return [(node, edge) for node, edge in rows]
