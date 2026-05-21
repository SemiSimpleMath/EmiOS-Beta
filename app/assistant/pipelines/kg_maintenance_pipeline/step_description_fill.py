"""
Step: description_fill

Generates descriptions for nodes that currently have none, prioritised by a
blended score of:

    priority = LENS_BLEND_IMPORTANCE_WEIGHT × node.importance
             + LENS_BLEND_PAGERANK_WEIGHT   × node.pagerank_score

Both fields default to 0.5 when NULL.  The top-N nodes (default 50) by this
score are processed in order — highest priority first — so LLM budget is
always spent where the graph needs it most.

Structural gate
---------------
  - Concept nodes are excluded (never useful for descriptions).
  - Entity nodes require >= 3 edges to filter conversational debris.
  - State nodes always kept (personality insights).
  - Event, Goal, Property nodes kept with >= 1 edge.

Cost control
------------
  max_nodes (default 50): hard cap per pipeline run.  Raise for a backfill,
  lower for a quick weekly pass.

Returns
-------
{"eligible": int, "processed": int, "updated": int, "errors": int}
"""
from __future__ import annotations

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.assistant.kg_core.kg_utils.node_importance import get_important_nodes
from app.assistant.importance.consumers import (
    LENS_BLEND_IMPORTANCE_WEIGHT,
    LENS_BLEND_PAGERANK_WEIGHT,
)
from app.models.base import get_session

logger = get_logger(__name__)


def _has_wiki_prose_page(label: str) -> bool:
    """Return True if a prose wiki page exists for this entity label.

    The wiki is the canonical source of node.description for entities that
    have a page — its lead paragraph IS the description (synced by
    page_writer._sync_lead_to_node_description). description_creator must
    not compete; it only fills descriptions for nodes the wiki doesn't cover.
    """
    if not label:
        return False
    try:
        from app.assistant.utils.filename_safety import safe_filename
        from app.assistant.wiki_generator.nightly_refresh import _default_vault
        prose_path = _default_vault() / "prose" / (safe_filename(label) + ".md")
        return prose_path.exists()
    except Exception:
        return False


def run(ctx: PipelineContext, max_nodes: int = 50) -> dict:
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.pipelines.kg_maintenance_pipeline.description_creator import (
        generate_description_for_node,
        persist_description,
    )

    # ── READ PHASE ────────────────────────────────────────────────────────────
    session = get_session()
    try:
        all_important = get_important_nodes(session, run_nano_filter=True)

        nodes_without_desc = set()
        wiki_owned_skipped = 0
        for snap in all_important:
            node = session.query(Node).filter(Node.id == snap.node_id).first()
            if not node:
                continue
            if node.description and node.description.strip():
                continue
            # Wiki guard: if a prose wiki page exists for this entity, the
            # wiki owns its description (lead paragraph → node.description
            # via page_writer._sync_lead_to_node_description). Skip here so
            # we don't write a competing description from raw neighborhood.
            if _has_wiki_prose_page(node.label):
                wiki_owned_skipped += 1
                continue
            nodes_without_desc.add(snap.node_id)

        work_queue = [s for s in all_important if s.node_id in nodes_without_desc]
    finally:
        session.close()

    eligible = len(all_important)

    logger.info(
        "┌─ step_description_fill ─────────────────────────────────────────",
    )
    logger.info(
        "│  important nodes: %d  |  missing description: %d  |  wiki-owned skipped: %d",
        eligible, len(work_queue), wiki_owned_skipped,
    )

    work_queue = work_queue[:max_nodes]
    logger.info(
        "│  processing=%d  (max_nodes=%d)", len(work_queue), max_nodes,
    )

    processed = 0
    updated = 0
    errors = 0

    for snap in work_queue:
        blend = LENS_BLEND_IMPORTANCE_WEIGHT * snap.importance + LENS_BLEND_PAGERANK_WEIGHT * snap.pagerank
        try:
            description = generate_description_for_node(snap.node_id)
            processed += 1
            if description:
                changed = persist_description(snap.node_id, description)
                if changed:
                    updated += 1
                    logger.info(
                        "│  DESCRIBED [%.3f] (%s) %r → %r…",
                        blend, snap.node_type, snap.label, description[:60],
                    )
            else:
                logger.info("│  EMPTY     [%.3f] (%s) %r — agent returned nothing", blend, snap.node_type, snap.label)
        except Exception:
            errors += 1
            logger.debug(
                "[step_description_fill] Failed node=%s label=%r", snap.node_id, snap.label, exc_info=True
            )

    logger.info(
        "└─ step_description_fill done  processed=%d updated=%d errors=%d",
        processed, updated, errors,
    )

    return {"eligible": eligible, "processed": processed, "updated": updated, "errors": errors}
