"""Importance-related queries that span more than one node.

This is the home for "compute importance over a neighborhood" helpers
that don't fit cleanly in any of the other layers. The canonical
example is `max_source_entity_importance` — given a State/Event/Goal
node, find the max importance of the Entity nodes that connect to it.

Used by the section-tagger gate to admit/reject candidate bullets:
a fact's "should it reach the card" decision looks at the max
importance among the Entities the fact is anchored to.

## Why these are queries, not gates

These functions return a number, not a yes/no. The caller decides what
to do with it (compare against a threshold, use as a weight, sort by
it, etc.). Gates that bake in a yes/no live in `consumers.py`.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    pass


def max_source_entity_importance(
    session: Session, node_id: str
) -> Optional[float]:
    """Return max importance among nodes that point AT ``node_id``.

    Moved verbatim from `kg/section_tagging._source_entity_importance`
    so callers other than the section tagger can use the same logic.
    The original location continues to work; section_tagging.py imports
    from here.

    Used by the section-tagger admission gate: a fact whose strongest
    "owner" (the Entity that has-state's this State/Event/Goal) is
    below an importance threshold is dropped unless overridden by other
    signals (e.g., contact-section tag).

    INTENTIONAL CARRY-OVER FROM ORIGINAL: the function name says
    "entity" but the SQL doesn't filter `Node.node_type == 'Entity'`.
    It considers any node with an edge pointing AT ``node_id``. In
    practice the incoming edges of a State/Event/Goal usually originate
    on an Entity (Entity --has_state--> State) so the Entity-only
    semantics emerge naturally. Preserved as-is to avoid changing
    behavior during unification; if a future caller needs a strict
    Entity filter, add a `node_type_filter=None` parameter then.

    Returns None when no incoming neighbor has populated importance —
    callers typically treat that as "no decision; pass through" rather
    than "zero importance."
    """
    # Local imports to keep this module light at startup.
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node

    rows = (
        session.query(Node.importance)
        .join(Edge, Edge.source_id == Node.id)
        .filter(Edge.target_id == node_id, Node.importance.isnot(None))
        .all()
    )
    if not rows:
        return None
    return max(float(r[0]) for r in rows)
