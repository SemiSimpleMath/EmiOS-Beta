"""
description_creator.py — writes a node's description without bumping updated_at.

Live callers: wiki_generator/page_writer.py (syncs the wiki lead into
node.description) and scripts/backfill_descriptions_from_wiki_leads.py.
The LLM description generator that lived here (and its edge pre-filter)
had no callers after step_description_fill was deleted 2026-06-10 (audit
P3.6) and was removed 2026-10-05.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def persist_description(node_id: str, description: str) -> bool:
    """
    Write description to the node row.  Returns True if the row was actually
    changed, False if the value was identical.

    Description is a downstream projection of the node's KG neighborhood
    (currently filled by either this maintenance pipeline or the wiki lead),
    not a structural property of the node itself. Writing it MUST NOT bump
    Node.updated_at — otherwise change_detection.find_changed_neighborhood_nodes
    sees the node as freshly modified and triggers a wiki refresh on every
    page that links to it, which in turn rewrites their leads, which bumps
    THEIR updated_at, cascading without bound.

    Implementation: Core UPDATE with `updated_at = Node.updated_at` self-
    reference. Explicitly providing a value for a column suppresses its
    `onupdate=func.now()` hook, so the timestamp is preserved verbatim.
    """
    from sqlalchemy import update
    from app.assistant.kg.db.knowledge_graph_db import Node

    write_session = get_session()
    try:
        current = (
            write_session.query(Node.description)
            .filter(Node.id == node_id)
            .scalar()
        )
        if current is None and not write_session.query(Node.id).filter(Node.id == node_id).scalar():
            logger.debug("[description_creator] Node %s not found at write time", node_id)
            return False
        if description == current:
            return False
        write_session.execute(
            update(Node)
            .where(Node.id == node_id)
            .values(description=description, updated_at=Node.updated_at)
        )
        write_session.commit()
        return True
    except Exception:
        write_session.rollback()
        logger.debug("[description_creator] Write failed node=%s", node_id, exc_info=True)
        raise
    finally:
        write_session.close()
