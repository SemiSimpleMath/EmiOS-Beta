"""Disambiguation nodes — attachment points for unresolved referents.

A Disambiguation node is a note to the KG: "when you don't know where to
add this edge, attach it HERE so a future investigator can take it and
add it to the right place." It sits at a label whose referent is
ambiguous — e.g. "Alex's Sister" when Alex has several sisters, or a
bare "Alex" once two distinct people share that label.

The promoter binds mentions at such a label directly to the
Disambiguation node: edges land ON it as real participant/property
edges, no new node is minted, no guess is made. That is the
anti-proliferation mechanism — ambiguity concentrates at one known
place instead of spawning "Alex's Sister", "Sister of Alex", … twins.

The maintenance loop drains it: the scanner raises a
``disambiguation_backlog`` finding when a Disambiguation node carries
edges; the investigator determines each edge's true referent (using the
anchor's relationship facts) and re-points it via ``kg_repoint_edge``.
Edges whose referent still can't be determined simply stay — an
edge-carrying Disambiguation is a legitimate waiting state, not an
error.

Disambiguation is NOT a forward: when a label has exactly one known
referent, the fix is an alias on the real node (the resolver already
matches aliases), not a marker node.

Disambiguation nodes are first-class KG nodes (node_type =
'Disambiguation'), so they're queryable, audit-friendly, and live where
the rest of the graph does. They have no temporal anchor of their own —
they're meta-graph correction records, not life-graph claims.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

DISAMBIGUATION_NODE_TYPE = "Disambiguation"


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


# ── Mint ──────────────────────────────────────────────────────────────────

def create_disambiguation(
    session: Any,
    *,
    label: str,
    reason: str = "",
) -> Any:
    """Mint a Disambiguation node at `label` — the attachment point for
    mentions whose true referent is unknown.

    Returns the new Node (already added to the session; caller commits).

    Idempotent: if a Disambiguation already exists at this label, the
    existing one is returned without re-creating.
    """
    from app.assistant.kg.db.knowledge_graph_db import Node

    existing = find_disambiguation(session, label)
    if existing is not None:
        return existing

    note_bits = [
        f"Disambiguation marker for label {label!r}: the referent is "
        f"ambiguous, so mentions at this label attach here until an "
        f"investigator re-points them to the right node.",
    ]
    if reason:
        note_bits.append(reason.strip())
    original_sentence = " ".join(note_bits)

    dis = Node(
        id=str(uuid.uuid4()),
        label=label,
        node_type=DISAMBIGUATION_NODE_TYPE,
        original_sentence=original_sentence,
        description=(
            f"Attachment point for label {label!r}: edges land here when "
            f"the true referent is unknown; the maintenance investigator "
            f"re-points them once the referent is determined."
        ),
    )
    session.add(dis)
    session.flush()

    logger.info(
        "[disambiguation] minted attachment point %s at label %r",
        dis.id[:8], label,
    )
    return dis
