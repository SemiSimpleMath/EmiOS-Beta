"""Per-entity timeline projection.

Walks every State/Event/Goal/Property attached to a focal entity that has a
date (ISO start_date OR start_date_prose), orders them chronologically, and
renders to markdown for agent grounding. Importance highlights surface the
meaningful events; everything else stays in the record for completeness.

Output target: ``resources/kg_derived/timelines/<slug>.md`` — agents can
inject these as a static resource, or a tool can fetch one on demand by
entity id or label.

Build is pure SQL + sort + format — no LLM calls. Cheap enough to rerun
nightly per entity.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import utc_now

logger = logging.getLogger(__name__)

# Node types eligible to appear on a timeline. Entity / Concept / Pod / Property
# don't carry time semantics meaningful to a life-event log.
_TIMELINE_NODE_TYPES = {"State", "Event", "Goal"}

# Importance bands used by the renderer. The cutoffs match the existing
# 0-10 scale; tweak here without touching downstream agents.
_BAND_MAJOR = 7.0   # ★ marker
_BAND_MID = 4.0     # · marker
# everything below _BAND_MID renders without a prefix (still included)

_OUTPUT_DIR = get_repo_root() / "resources" / "kg_derived" / "timelines"


@dataclass
class TimelineEntry:
    """One row in the rendered timeline."""
    date_iso: Optional[str]            # 'YYYY-MM-DD' or None when only prose available
    date_prose: Optional[str]          # e.g. "around 2010" — populated when iso is None
    # Confidence on the start_date: 'actual' = user-confirmed or explicit in
    # evidence; 'inferred' = derived from clear context; 'estimated' = derived
    # with relative anchors / fuzzy. The renderer marks non-actual visually so
    # agents reading the timeline can see at a glance what's confirmed vs guessed.
    date_confidence: Optional[str]
    end_iso: Optional[str]             # for ranges; None for point-in-time
    end_prose: Optional[str]
    end_confidence: Optional[str]
    label: str
    sentence: str
    node_type: str
    category: Optional[str]
    importance: float                  # 0-10
    relationship_type: str             # edge label connecting to focal entity
    direction: str                     # 'incoming' | 'outgoing'

    def sort_key(self) -> str:
        """Sort by ISO date first, then by prose anchor for the dateless tail.
        Returns a string key that sorts ISO dates correctly + groups
        ISO-less entries after them.
        """
        if self.date_iso:
            return self.date_iso  # 'YYYY-MM-DD' sorts lexicographically
        # No ISO: push to end, sort prose lexicographically among themselves.
        return "9999-99-99|" + (self.date_prose or "")


def _slugify(text: str) -> str:
    """File-safe slug from an entity label."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_").lower()
    return s or "untitled"


def _format_one_date(iso: Optional[str], prose: Optional[str], confidence: Optional[str]) -> str:
    """Render a single date with a confidence marker.

    Suffix conventions (visible at a glance to any agent reading the file):
      ``YYYY-MM-DD``         actual — user-confirmed or evidence-explicit
      ``YYYY-MM-DD?``        inferred — derived from clear context
      ``YYYY-MM-DD?est``     estimated — derived with fuzzy / relative anchors
      ``prose``              prose only — no ISO available, only narrative anchor

    The point: a date-fill agent that writes back to Node.start_date with
    confidence='estimated' MUST be visible in the rendered output. Without a
    visible marker, an agent reading the timeline can't tell what's confirmed
    from what's guessed and would compound the guesswork.
    """
    if iso:
        c = (confidence or "").lower()
        if c in ("", "actual"):
            return iso
        if c == "inferred":
            return f"{iso}?"
        if c == "estimated":
            return f"{iso}?est"
        # Unknown confidence string — be defensive; mark unclear.
        return f"{iso}?{c}"
    if prose:
        return prose
    return "—"


def _date_column(entry: TimelineEntry) -> str:
    """Render the date column. ISO when known (with confidence marker),
    prose when not, range with arrow when both ends present."""
    start = _format_one_date(entry.date_iso, entry.date_prose, entry.date_confidence)
    end = _format_one_date(entry.end_iso, entry.end_prose, entry.end_confidence)
    if (entry.end_iso or entry.end_prose) and end != start:
        return f"{start} → {end}"
    return start


def _importance_marker(imp: Optional[float]) -> str:
    """Visual weight for the importance column. Falls back to bare entry
    when importance is unrated (None) — common during transition."""
    if imp is None:
        return "  "
    if imp >= _BAND_MAJOR:
        return "★ "
    if imp >= _BAND_MID:
        return "· "
    return "  "


def collect_entries(session: Session, entity_id: str) -> List[TimelineEntry]:
    """Walk every State/Event/Goal connected to ``entity_id`` that has a
    date (ISO or prose). Returns chronologically sorted entries."""
    entity = session.query(Node).filter(Node.id == entity_id).first()
    if entity is None:
        return []

    edges = (
        session.query(Edge)
        .filter((Edge.source_id == entity_id) | (Edge.target_id == entity_id))
        .all()
    )

    entries: List[TimelineEntry] = []
    seen_node_ids = set()
    for edge in edges:
        is_outgoing = edge.source_id == entity_id
        other_id = edge.target_id if is_outgoing else edge.source_id
        if other_id in seen_node_ids:
            continue
        other = session.get(Node, other_id)
        if other is None:
            continue
        if (other.node_type or "") not in _TIMELINE_NODE_TYPES:
            continue
        # Need at least one of: start_date (ISO) or start_date_prose
        has_iso = other.start_date is not None
        has_prose = bool((other.start_date_prose or "").strip())
        if not has_iso and not has_prose:
            continue

        seen_node_ids.add(other_id)
        entries.append(TimelineEntry(
            date_iso=other.start_date.date().isoformat() if has_iso else None,
            date_prose=(other.start_date_prose or "").strip() or None,
            date_confidence=(other.start_date_confidence or "").strip().lower() or None,
            end_iso=other.end_date.date().isoformat() if other.end_date else None,
            end_prose=(other.end_date_prose or "").strip() or None,
            end_confidence=(other.end_date_confidence or "").strip().lower() or None,
            label=other.label or "?",
            sentence=(other.original_sentence or "").strip(),
            node_type=other.node_type or "?",
            category=other.category,
            importance=float(other.importance) if other.importance is not None else None,
            relationship_type=edge.relationship_type or "?",
            direction="outgoing" if is_outgoing else "incoming",
        ))

    entries.sort(key=lambda e: e.sort_key())
    return entries


def render_markdown(entity: Node, entries: List[TimelineEntry]) -> str:
    """Format the entity's timeline as markdown.

    Layout: a Date | Importance-prefixed-label header + one line per entry
    rendering the canonical sentence (or the label when sentence is empty).
    Closed states / past events render with their full date range.
    """
    header_lines = [
        f"# Timeline — {entity.label or entity.id}",
        "",
        f"*Generated {utc_now().date().isoformat()} from {len(entries)} dated "
        f"{entity.node_type or 'entity'}-connected fact{'s' if len(entries) != 1 else ''}.*",
        "",
        f"Importance: ★ = major (≥ {_BAND_MAJOR}); "
        f"· = moderate ({_BAND_MID}–{_BAND_MAJOR}); "
        f"unmarked = minor/unrated.",
        "Date confidence: `YYYY-MM-DD` = actual; `YYYY-MM-DD?` = inferred; "
        "`YYYY-MM-DD?est` = estimated; prose-only when no ISO known.",
        "",
    ]
    body_lines: List[str] = []
    last_year = None
    for e in entries:
        date_str = _date_column(e)
        year = (e.date_iso or "")[:4]
        if year and year != last_year:
            body_lines.append("")
            body_lines.append(f"## {year}")
            last_year = year
        marker = _importance_marker(e.importance)
        # Prefer the canonical sentence; fall back to label when missing.
        text = e.sentence or e.label
        body_lines.append(f"- `{date_str}` {marker}{text}")

    return "\n".join(header_lines + body_lines) + "\n"


def build_entity_timeline(entity_id: str, session: Optional[Session] = None) -> str:
    """Build the timeline for ``entity_id`` and return the markdown text.
    Caller owns the session (if passed) or this function manages one."""
    owns_session = session is None
    if owns_session:
        from app.models.base import get_session
        session = get_session()
    try:
        entity = session.query(Node).filter(Node.id == entity_id).first()
        if entity is None:
            return f"# Timeline — (unknown entity {entity_id})\n\n*Not found.*\n"
        entries = collect_entries(session, entity_id)
        return render_markdown(entity, entries)
    finally:
        if owns_session:
            session.close()


def persist_entity_timeline(entity_id: str) -> Optional[Path]:
    """Build + write the timeline to
    ``resources/kg_derived/timelines/<slug>.md``. Returns the path written
    (or None if the entity wasn't found)."""
    from app.models.base import get_session
    session = get_session()
    try:
        entity = session.query(Node).filter(Node.id == entity_id).first()
        if entity is None:
            return None
        md = build_entity_timeline(entity_id, session=session)
    finally:
        session.close()

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(entity.label or entity.id)
    out_path = _OUTPUT_DIR / f"{slug}.md"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(md, encoding="utf-8")
    tmp.replace(out_path)
    logger.info("[timeline_builder] wrote %s", out_path)
    return out_path
