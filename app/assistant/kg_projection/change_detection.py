"""Change detection for incremental refresh.

Given an entity id and a "since" timestamp, return the ids of neighborhood
nodes whose ``updated_at`` is newer than the cutoff. Used by wiki's nightly
refresh (to decide which sections need rewriting) and will be used by the
entity-card regen path for the same purpose.

The neighborhood here includes:
- The entity itself.
- Every directly-connected node (State/Event/Goal/Entity/misc).
- For State/Event hubs, counterpart entities one hop further out, since
  their changes can shift how the hub bullet reads.

Edges are also considered: if an edge touching the entity updated after the
cutoff, its non-entity endpoint is returned as a changed node.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from app.assistant.kg.db.knowledge_graph_db import Edge, Node, get_session
from app.assistant.pod_store.pod_uri import POD_URI_RE


def find_changed_neighborhood_nodes(
    *,
    entity_node_id: str,
    since_ts: datetime,
) -> List[str]:
    """Return ids of KG nodes (or pod URIs) in the entity's neighborhood
    with ``updated_at > since_ts``.

    The neighborhood is the entity itself plus every state/event/goal/concept
    node directly connected by an edge, plus the remote endpoint entities.
    Edge-updated-without-node-updated is also considered: if an edge touching
    the entity has ``updated_at > since_ts``, the non-entity endpoint of that
    edge is returned as a changed node.

    Pod URIs (``datapod:<kind>:<hash>``) are valid edge endpoints with no
    corresponding kg_node row. We can't query pod_store for an updated_at,
    so for pods we use edge-level change as the only signal: if the edge
    touching a pod changed, the pod URI is reported as a changed neighbor
    so downstream wiki/card refresh re-renders the bullet.
    """
    session = get_session()
    try:
        entity = session.query(Node).filter(Node.id == entity_node_id).first()
        if entity is None:
            return []

        changed: set[str] = set()
        if entity.updated_at and entity.updated_at.replace(tzinfo=timezone.utc) > since_ts:
            changed.add(entity.id)

        edges_out = session.query(Edge).filter(Edge.source_id == entity_node_id).all()
        edges_in = session.query(Edge).filter(Edge.target_id == entity_node_id).all()
        for e in edges_out + edges_in:
            other_id = e.target_id if e.source_id == entity_node_id else e.source_id
            other = session.query(Node).filter(Node.id == other_id).first()
            other_is_pod = other is None and bool(POD_URI_RE.fullmatch(other_id or ""))
            edge_changed = bool(e.updated_at) and e.updated_at.replace(tzinfo=timezone.utc) > since_ts

            # Edge-level change → both real-node and pod-URI endpoints qualify
            if edge_changed:
                if other is not None:
                    changed.add(other.id)
                elif other_is_pod:
                    changed.add(other_id)

            # Node-level change on the other endpoint (skip pods — no kg_node)
            if other is not None and other.updated_at and \
                    other.updated_at.replace(tzinfo=timezone.utc) > since_ts:
                changed.add(other.id)

            # Hub expansion only meaningful for State/Event kg_nodes, not pods.
            if other is not None and other.node_type in {"State", "Event"}:
                hub_edges = (
                    session.query(Edge)
                    .filter((Edge.source_id == other.id) | (Edge.target_id == other.id))
                    .all()
                )
                for he in hub_edges:
                    hid = he.target_id if he.source_id == other.id else he.source_id
                    if hid == entity_node_id:
                        continue
                    counterpart = session.query(Node).filter(Node.id == hid).first()
                    if counterpart is not None and counterpart.updated_at and \
                            counterpart.updated_at.replace(tzinfo=timezone.utc) > since_ts:
                        # A counterpart changed → the state bullet is stale
                        changed.add(other.id)
                    if he.updated_at and he.updated_at.replace(tzinfo=timezone.utc) > since_ts:
                        changed.add(other.id)

        return sorted(changed)
    finally:
        session.close()
