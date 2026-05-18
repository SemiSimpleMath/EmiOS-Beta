"""Surface gap-shaped conversation hooks from a user's KG timeline.

Reads the same data the timeline_builder uses, but returns a small,
structured set of question candidates the conversation_starter can use:

  - estimated_dates: high-importance facts with `?est` confidence — worth
    confirming with the user ("I have your wedding as around Sept 2003 —
    was that the exact date?").
  - year_gaps: multi-year stretches with no dated facts — worth probing
    ("I have nothing between 1990 and 2001 — what were you up to then?").
  - prose_only: facts that only have prose anchors, no ISO — worth pinning
    ("you mentioned moving to Irvine after Peter was born — when exactly?").

Pure SQL + arithmetic. Zero LLM cost. Cheap enough to recompute per call.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node


# Importance gate for `?est` candidates worth bothering the user about.
# Lower-importance estimates aren't worth surfacing — confirming "Katy was
# upstairs around March 2026" is noise.
from app.assistant.importance.consumers import TIMELINE_CONFIRMATION_FLOOR
from app.assistant.importance.effective import effective_importance

# Years gap >= this many → consider "wide coverage hole" worth asking about.
_MIN_GAP_YEARS = 5

# Cap on candidates returned per category so the prompt context stays small.
_MAX_PER_CATEGORY = 3


@dataclass
class GapCandidate:
    """One conversation hook surfaced from the timeline."""
    kind: str          # 'estimated_date' | 'year_gap' | 'prose_only'
    node_id: Optional[str]
    label: Optional[str]
    sentence: Optional[str]
    importance: Optional[float]
    # Hints for the conversation_starter prompt:
    hint: str          # natural-language description of the gap
    suggested_frame: str  # how to phrase the question

    def to_dict(self) -> dict:
        return asdict(self)


def _candidates_for_entity(session: Session, entity_id: str) -> Dict[str, List[GapCandidate]]:
    """Walk the entity's dated neighborhood and bucket candidates."""
    entity = session.query(Node).filter(Node.id == entity_id).first()
    if entity is None:
        return {"estimated_dates": [], "year_gaps": [], "prose_only": []}

    # Collect all dated nodes connected to entity (mirrors timeline_builder).
    edges = (
        session.query(Edge)
        .filter((Edge.source_id == entity_id) | (Edge.target_id == entity_id))
        .all()
    )
    seen: set = set()
    dated: List[Node] = []
    for e in edges:
        other_id = e.target_id if e.source_id == entity_id else e.source_id
        if other_id in seen:
            continue
        seen.add(other_id)
        other = session.get(Node, other_id)
        if other is None:
            continue
        if (other.node_type or "") not in {"State", "Event", "Goal"}:
            continue
        has_iso = other.start_date is not None
        has_prose = bool((other.start_date_prose or "").strip())
        if not has_iso and not has_prose:
            continue
        dated.append(other)

    # 1) Estimated dates (high-importance + non-actual confidence)
    estimated: List[GapCandidate] = []
    for n in dated:
        if n.start_date is None:
            continue
        c = (n.start_date_confidence or "").lower()
        if c in ("", "actual"):
            continue
        if c not in ("estimated", "inferred"):
            continue
        if effective_importance(n) < TIMELINE_CONFIRMATION_FLOOR:
            continue
        date_str = n.start_date.date().isoformat() if n.start_date else "?"
        estimated.append(GapCandidate(
            kind="estimated_date",
            node_id=n.id,
            label=n.label,
            sentence=(n.original_sentence or "").strip(),
            importance=float(n.importance) if n.importance else None,
            hint=f"Date currently {c} as {date_str}",
            suggested_frame=(
                f"You can ask the user to confirm: 'I have this as around "
                f"{date_str} — was that the exact date?' Don't ask blindly; "
                f"ground it in the specific fact."
            ),
        ))
    estimated.sort(key=lambda g: -(g.importance or 0))
    estimated = estimated[:_MAX_PER_CATEGORY]

    # 2) Year gaps — find multi-year stretches with no events
    years = sorted({
        n.start_date.year for n in dated if n.start_date is not None
    })
    year_gaps: List[GapCandidate] = []
    if len(years) >= 2:
        for i in range(1, len(years)):
            gap = years[i] - years[i - 1]
            if gap >= _MIN_GAP_YEARS:
                year_gaps.append(GapCandidate(
                    kind="year_gap",
                    node_id=None,
                    label=f"{years[i-1]}-{years[i]}",
                    sentence=None,
                    importance=None,
                    hint=f"No dated events between {years[i-1]} and {years[i]}",
                    suggested_frame=(
                        f"You can ask: 'I don't have much about your life "
                        f"between {years[i-1]} and {years[i]} — anything "
                        f"stand out from that stretch?'"
                    ),
                ))
    year_gaps.sort(key=lambda g: -(int(g.label.split('-')[1]) - int(g.label.split('-')[0])))
    year_gaps = year_gaps[:_MAX_PER_CATEGORY]

    # 3) Prose-only facts that are important — could pin to ISO
    prose_only: List[GapCandidate] = []
    for n in dated:
        if n.start_date is not None:
            continue  # has ISO already
        if not (n.start_date_prose or "").strip():
            continue
        if effective_importance(n) < TIMELINE_CONFIRMATION_FLOOR:
            continue
        prose_only.append(GapCandidate(
            kind="prose_only",
            node_id=n.id,
            label=n.label,
            sentence=(n.original_sentence or "").strip(),
            importance=float(n.importance) if n.importance else None,
            hint=f"Only prose anchor: '{n.start_date_prose}'",
            suggested_frame=(
                f"You can ask the user to pin the date: e.g. 'when did "
                f"that happen exactly?' but be specific about which event."
            ),
        ))
    prose_only.sort(key=lambda g: -(g.importance or 0))
    prose_only = prose_only[:_MAX_PER_CATEGORY]

    return {
        "estimated_dates": estimated,
        "year_gaps": year_gaps,
        "prose_only": prose_only,
    }


def compute_timeline_gaps(entity_id: str) -> Dict[str, List[dict]]:
    """Public entry: returns dict with three lists of candidate dicts.

    Shape:
      {
        "estimated_dates": [GapCandidate.to_dict(), ...],
        "year_gaps": [...],
        "prose_only": [...],
      }
    Each list is capped at _MAX_PER_CATEGORY entries, ranked by importance
    (or gap size for year_gaps).
    """
    from app.models.base import get_session
    session = get_session()
    try:
        buckets = _candidates_for_entity(session, entity_id)
    finally:
        session.close()
    return {k: [c.to_dict() for c in v] for k, v in buckets.items()}
