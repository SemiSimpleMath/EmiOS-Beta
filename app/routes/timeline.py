"""Per-entity timeline UI.

Two routes:
  GET /timeline/<entity_id>      — renders the timeline page for one entity
  GET /api/timeline/<entity_id>  — returns the entity's timeline as JSON

The JSON shape is consumed by the embedded SVG renderer in
``templates/timeline.html``. Heavy lifting (KG walk + importance + date
math) is in ``app/assistant/kg/timeline_builder.py``; this module is the
thin web surface that calls it and serves the page.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg.timeline_builder import collect_entries, _slugify
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

timeline_bp = Blueprint("timeline", __name__)


@timeline_bp.route("/timelines", methods=["GET"])
def timelines_index():
    """Chooser page — lists entities that have enough dated facts to make a
    meaningful timeline. Sorted by importance descending."""
    session = get_session()
    try:
        entities = (
            session.query(Node)
            .filter(Node.node_type == "Entity")
            .filter(Node.importance.isnot(None))
            .order_by(Node.importance.desc().nullslast())
            .limit(60)
            .all()
        )
        listings: List[Dict[str, Any]] = []
        for e in entities:
            try:
                entries = collect_entries(session, e.id)
            except Exception:
                entries = []
            if not entries:
                continue
            slug = _slugify(e.label or e.id)
            listings.append({
                "id": e.id,
                "label": e.label,
                "category": e.category or "",
                "importance": float(e.importance) if e.importance is not None else None,
                "fact_count": len(entries),
                "slug": slug,
                "href": f"/timeline/{slug}",
            })
        return render_template("timelines_index.html", listings=listings)
    finally:
        session.close()



def _resolve_entity(session, key: str) -> Optional[Node]:
    """Accept either a UUID id or a slug (matches _slugify(label))."""
    # Try id first
    node = session.query(Node).filter(Node.id == key).first()
    if node is not None:
        return node
    # Slug lookup against entity persons (most common case)
    # Walk entities and match on slug — not a hot path, fine for now.
    candidates = (
        session.query(Node)
        .filter(Node.node_type == "Entity")
        .filter(Node.label.isnot(None))
        .all()
    )
    for c in candidates:
        if _slugify(c.label or "") == key.lower():
            return c
    return None


@timeline_bp.route("/timeline/<entity_key>", methods=["GET"])
def timeline_page(entity_key: str):
    """Render the timeline page for an entity (by id or label-slug)."""
    session = get_session()
    try:
        entity = _resolve_entity(session, entity_key)
        if entity is None:
            return render_template(
                "timeline.html",
                entity_label="(unknown)",
                entity_id="",
                error=f"No entity found for '{entity_key}'.",
            ), 404
        return render_template(
            "timeline.html",
            entity_label=entity.label or "(unnamed)",
            entity_id=entity.id,
            entity_type=entity.node_type or "Entity",
            entity_importance=float(entity.importance) if entity.importance is not None else None,
            entity_description=entity.description or "",
            error=None,
        )
    finally:
        session.close()


@timeline_bp.route("/api/timeline/<entity_key>", methods=["GET"])
def timeline_data(entity_key: str):
    """Return the entity's dated facts as JSON for the SVG renderer."""
    session = get_session()
    try:
        entity = _resolve_entity(session, entity_key)
        if entity is None:
            return jsonify({"ok": False, "error": "not_found"}), 404

        entries = collect_entries(session, entity.id)

        # Convert TimelineEntry dataclasses to dicts the JS renderer can use.
        # Compute a parsed numeric year + ISO for layout (use the start date
        # as the anchor; ranges keep both ends).
        out: List[Dict[str, Any]] = []
        for e in entries:
            year = None
            if e.date_iso:
                try:
                    year = int(e.date_iso[:4])
                except ValueError:
                    year = None
            out.append({
                "date_iso": e.date_iso,
                "date_prose": e.date_prose,
                "date_confidence": e.date_confidence,
                "end_iso": e.end_iso,
                "end_prose": e.end_prose,
                "end_confidence": e.end_confidence,
                "year": year,
                "label": e.label,
                "sentence": e.sentence,
                "node_type": e.node_type,
                "category": e.category,
                "importance": e.importance,
                "relationship_type": e.relationship_type,
                "direction": e.direction,
            })

        # Min/max year for axis bounds — fall back to a reasonable window
        # when no dated events have ISO dates yet.
        years = [r["year"] for r in out if r["year"] is not None]
        min_year = min(years) if years else 1970
        max_year = max(years) if years else 2030

        return jsonify({
            "ok": True,
            "entity": {
                "id": entity.id,
                "label": entity.label,
                "node_type": entity.node_type,
                "importance": float(entity.importance) if entity.importance is not None else None,
            },
            "entries": out,
            "min_year": min_year,
            "max_year": max_year,
            "total": len(out),
        })
    finally:
        session.close()
