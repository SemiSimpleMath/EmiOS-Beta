"""Deterministic KG-to-bullet rendering with provenance.

Walks an ``EntityNeighborhood`` and produces a flat list of ``Bullet``
dataclasses. Each bullet has:

- ``text``: the rendered markdown text (multiline for bullets with a quoted
  source sentence, metadata bracket, or KG-gap comment children).
- ``kind``: which neighborhood bucket the bullet came from.
- ``subkind``: finer partition within a kind (e.g. dated vs undated
  relationships, active vs other goals). Used by downstream renderers that
  split a kind into multiple sub-sections.
- ``sort_key``: the bullet's start_date (if any), normalized to UTC, for
  reverse-chronological sorting.
- ``source_node_ids`` / ``source_edge_ids``: explicit provenance, replaces
  the ``{node:<uuid>}`` regex scan the old page_writer used for detecting
  which bullets touch a changed node.
- ``key``: content hash of ``text`` for the tag cache.

This module is the single source of truth for bullet text. Consumers (wiki
renderer, entity card writer, tagger) all read ``Bullet.text`` — no one
re-parses markdown to get back to bullets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from app.assistant.kg_projection.neighborhood import (
    BeliefAbout,
    EntityNeighborhood,
    EventConnection,
    GoalTargeting,
    StateConnection,
)
from app.assistant.kg_projection.tag_cache import bullet_key


BulletKind = str  # 'relationship' | 'event' | 'belief' | 'goal' | 'misc_outgoing' | 'misc_incoming'
BulletSubkind = Optional[str]  # e.g. 'dated' / 'undated' / 'active' / 'other'


@dataclass
class Bullet:
    text: str
    kind: BulletKind
    source_node_ids: List[str] = field(default_factory=list)
    source_edge_ids: List[str] = field(default_factory=list)
    subkind: BulletSubkind = None
    sort_key: Optional[datetime] = None
    # Window IDs that produced the source nodes/edges. Used by downstream
    # LLM steps that want the full resolved chat window as grounding context
    # (the tagger, primarily). A bullet typically has one window — multiple
    # only when its provenance spans facts from different sessions.
    source_window_ids: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Content hash for caching — same text → same key."""
        return bullet_key(self.text)


# ---------- public entry point ----------


def render_bullets(neighborhood: EntityNeighborhood) -> List[Bullet]:
    """Render every neighborhood fact as a structured ``Bullet`` with provenance.

    Order is: relationships (dated desc, then undated), events (dated desc,
    then undated), beliefs, active goals, other goals, misc_outgoing,
    misc_incoming. Downstream renderers can regroup by ``kind``/``subkind``
    as they see fit.
    """
    entity = neighborhood.entity
    entity_id = str(entity.id) if getattr(entity, "id", None) else None
    entity_label = getattr(entity, "label", "") or ""
    out: List[Bullet] = []

    # ---- relationships (State connections, outbound) ----
    dated_rel = sorted(
        [r for r in neighborhood.relationships if r.state.start_date is not None],
        key=lambda r: _sort_key_ts(r.state.start_date),
        reverse=True,
    )
    undated_rel = [r for r in neighborhood.relationships if r.state.start_date is None]
    for r in dated_rel:
        out.append(_state_connection_bullet(r, subkind="dated", entity_label=entity_label))
    for r in undated_rel:
        out.append(_state_connection_bullet(r, subkind="undated", entity_label=entity_label))

    # ---- inbound relationships (this entity is the target of a State hub) ----
    in_dated_rel = sorted(
        [r for r in neighborhood.inbound_relationships if r.state.start_date is not None],
        key=lambda r: _sort_key_ts(r.state.start_date),
        reverse=True,
    )
    in_undated_rel = [r for r in neighborhood.inbound_relationships if r.state.start_date is None]
    for r in in_dated_rel:
        out.append(_state_connection_bullet(r, subkind="dated", entity_label=entity_label))
    for r in in_undated_rel:
        out.append(_state_connection_bullet(r, subkind="undated", entity_label=entity_label))

    # ---- events (outbound) ----
    dated_evt = sorted(
        [e for e in neighborhood.events if e.event.start_date is not None],
        key=lambda e: _sort_key_ts(e.event.start_date),
        reverse=True,
    )
    undated_evt = [e for e in neighborhood.events if e.event.start_date is None]
    for e in dated_evt:
        out.append(_event_connection_bullet(e, subkind="dated", entity_label=entity_label))
    for e in undated_evt:
        out.append(_event_connection_bullet(e, subkind="undated", entity_label=entity_label))

    # ---- inbound events (this entity is the location/recipient of an Event hub) ----
    in_dated_evt = sorted(
        [e for e in neighborhood.inbound_events if e.event.start_date is not None],
        key=lambda e: _sort_key_ts(e.event.start_date),
        reverse=True,
    )
    in_undated_evt = [e for e in neighborhood.inbound_events if e.event.start_date is None]
    for e in in_dated_evt:
        out.append(_event_connection_bullet(e, subkind="dated", entity_label=entity_label))
    for e in in_undated_evt:
        out.append(_event_connection_bullet(e, subkind="undated", entity_label=entity_label))

    # ---- beliefs ----
    for b in neighborhood.beliefs_about:
        out.append(_belief_bullet(b))

    # ---- goals ----
    active_goals = [g for g in neighborhood.goals_targeting
                    if (g.goal.goal_status or "").lower() == "active"]
    other_goals = [g for g in neighborhood.goals_targeting
                   if (g.goal.goal_status or "").lower() != "active"]
    for g in active_goals:
        out.append(_goal_bullet(g, subkind="active"))
    for g in other_goals:
        out.append(_goal_bullet(g, subkind="other"))

    # ---- misc edges ----
    for edge, target in neighborhood.misc_outgoing:
        out.append(_misc_outgoing_bullet(edge, target))
    for source, edge in neighborhood.misc_incoming:
        out.append(_misc_incoming_bullet(source, edge))

    return out


# ---------- bullet builders ----------


def _collect_window_ids(*sources) -> List[str]:
    """Pull unique window_ids from a series of nodes/edges. Returns dedupped list."""
    seen: set[str] = set()
    out: List[str] = []
    for s in sources:
        if s is None:
            continue
        wid = getattr(s, "window_id", None)
        if not wid:
            continue
        wid_str = str(wid)
        if wid_str in seen:
            continue
        seen.add(wid_str)
        out.append(wid_str)
    return out


def _role_line(node_label: str, edge) -> str:
    """Render one participant's role in a State/Event hub.

    Each participant has its OWN edge with its OWN relationship_type and
    sentence, so the bullet must show that per-edge breakdown rather than
    flattening counterparts under the rendering entity's predicate.
    """
    rel = (getattr(edge, "relationship_type", "") or "").replace("_", " ").lower()
    sentence = (getattr(edge, "sentence", "") or "").strip()
    sentence_str = f' — "{sentence}"' if sentence else ""
    return f"    - [[{_slugify(node_label)}]] — {rel}{sentence_str}"


def _state_connection_bullet(r: StateConnection, *, subkind: str, entity_label: str) -> Bullet:
    state = r.state
    edge = r.edge
    date = _fmt_date(state.start_date)
    date_str = f" since {date}" if date else ""
    node_tag = f" {{node:{state.id}}}" if getattr(state, "id", None) else ""

    # Header: state label + dates + node tag. The role list below carries the
    # per-edge predicate info — flattening it into the header (the old shape)
    # falsely attributed every counterpart's role to the rendering entity.
    lines = [f"- *{state.label}*{date_str}{node_tag}"]

    fact_sentence = (state.original_sentence or "").strip() or (edge.sentence or "").strip()
    if fact_sentence:
        lines.append(f"  > {fact_sentence}")

    role_lines: List[str] = [_role_line(entity_label, edge)]
    for c_node, c_edge in r.counterparts:
        role_lines.append(_role_line(getattr(c_node, "label", "") or "", c_edge))
    lines.append("  Roles in this state:")
    lines.extend(role_lines)

    meta_bits = _read_metadata_bits(state, edge)
    if meta_bits:
        lines.append(f"  [{meta_bits}]")

    counterpart_names = [c.label for c, _ in r.counterparts]
    if len(r.counterparts) > 1:
        for a, b in _detect_likely_duplicates(counterpart_names):
            lines.append(f"  <!-- KG GAP: state {state.label!r} likely duplicate counterparts: {a!r} and {b!r} -->")
    if state.start_date is None and state.end_date is None:
        lines.append(f"  <!-- KG GAP: state {state.label!r} has no start_date or end_date -->")

    source_node_ids = [str(state.id)] if getattr(state, "id", None) else []
    source_edge_ids = [str(edge.id)] if getattr(edge, "id", None) else []

    return Bullet(
        text="\n".join(lines),
        kind="relationship",
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        source_window_ids=_collect_window_ids(state, edge),
        subkind=subkind,
        sort_key=_normalize_dt(state.start_date),
    )


def _event_connection_bullet(e: EventConnection, *, subkind: str, entity_label: str) -> Bullet:
    event = e.event
    edge = e.edge
    date = _fmt_date(event.start_date)
    date_str = f" ({date})" if date else ""
    node_tag = f" {{node:{event.id}}}" if getattr(event, "id", None) else ""

    lines = [f"- **{event.label}**{date_str}{node_tag}"]

    fact = (event.original_sentence or "").strip() or (edge.sentence or "").strip()
    if fact:
        lines.append(f"  > {fact}")

    role_lines: List[str] = [_role_line(entity_label, edge)]
    for c_node, c_edge in e.co_participants:
        role_lines.append(_role_line(getattr(c_node, "label", "") or "", c_edge))
    lines.append("  Roles in this event:")
    lines.extend(role_lines)

    meta_bits = _read_metadata_bits(event, edge)
    if meta_bits:
        lines.append(f"  [{meta_bits}]")

    if event.start_date is None:
        lines.append(f"  <!-- KG GAP: event {event.label!r} has no start_date -->")

    source_node_ids = [str(event.id)] if getattr(event, "id", None) else []
    source_edge_ids = [str(edge.id)] if getattr(edge, "id", None) else []

    return Bullet(
        text="\n".join(lines),
        kind="event",
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        source_window_ids=_collect_window_ids(event, edge),
        subkind=subkind,
        sort_key=_normalize_dt(event.start_date),
    )


def _belief_bullet(b: BeliefAbout) -> Bullet:
    believer = f"[[{_slugify(b.believer.label)}]]" if b.believer else "*(unknown believer)*"
    node_tag = f" {{node:{b.state.id}}}" if getattr(b.state, "id", None) else ""
    lines = [f"- {believer}: **{b.state.label}**{node_tag}"]
    fact = (b.state.original_sentence or "").strip() or (b.edge_in.sentence or "").strip()
    if fact:
        lines.append(f"  > {fact}")

    source_node_ids = [str(b.state.id)] if getattr(b.state, "id", None) else []
    source_edge_ids = [str(b.edge_in.id)] if getattr(b.edge_in, "id", None) else []
    return Bullet(
        text="\n".join(lines),
        kind="belief",
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        source_window_ids=_collect_window_ids(b.state, b.edge_in),
    )


def _goal_bullet(g: GoalTargeting, *, subkind: str) -> Bullet:
    owner = f"[[{_slugify(g.owner.label)}]]" if g.owner else "*(unknown owner)*"
    node_tag = f" {{node:{g.goal.id}}}" if getattr(g.goal, "id", None) else ""
    if subkind == "active":
        lines = [f"- {owner}: {g.goal.label}{node_tag}"]
        fact = (g.goal.original_sentence or "").strip() or (g.edge_in.sentence or "").strip()
        if fact:
            lines.append(f"  > {fact}")
    else:
        status = g.goal.goal_status or "unknown"
        lines = [f"- {owner}: {g.goal.label} *(status: {status})*{node_tag}"]

    source_node_ids = [str(g.goal.id)] if getattr(g.goal, "id", None) else []
    source_edge_ids = [str(g.edge_in.id)] if getattr(g.edge_in, "id", None) else []
    return Bullet(
        text="\n".join(lines),
        kind="goal",
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        source_window_ids=_collect_window_ids(g.goal, g.edge_in),
        subkind=subkind,
    )


def _misc_outgoing_bullet(edge, target) -> Bullet:
    rel = (edge.relationship_type or "relates_to").replace("_", " ").lower()
    target_label = getattr(target, "label", "") or "(unlabeled)"
    node_tag = f" {{node:{target.id}}}" if getattr(target, "id", None) else ""
    text = f"- **{rel}** [[{_slugify(target_label)}]]{node_tag}"
    source_node_ids = [str(target.id)] if getattr(target, "id", None) else []
    source_edge_ids = [str(edge.id)] if getattr(edge, "id", None) else []
    return Bullet(
        text=text,
        kind="misc_outgoing",
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        source_window_ids=_collect_window_ids(target, edge),
    )


def _misc_incoming_bullet(source, edge) -> Bullet:
    rel = (edge.relationship_type or "relates_to").replace("_", " ").lower()
    source_label = getattr(source, "label", "") or "(unlabeled)"
    node_tag = f" {{node:{source.id}}}" if getattr(source, "id", None) else ""
    text = f"- [[{_slugify(source_label)}]] **{rel}** this{node_tag}"
    source_node_ids = [str(source.id)] if getattr(source, "id", None) else []
    source_edge_ids = [str(edge.id)] if getattr(edge, "id", None) else []
    return Bullet(
        text=text,
        kind="misc_incoming",
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        source_window_ids=_collect_window_ids(source, edge),
    )


# ---------- pure helpers (duplicated from wiki_renderer for now;
#             a later pass can have wiki_renderer consume these bullets
#             directly and remove the duplication) ----------


def _slugify(label: str) -> str:
    return (label or "").strip()


def _fmt_date(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(dt)


def _sort_key_ts(dt: Optional[datetime]) -> float:
    if dt is None:
        return 0.0
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_metadata_bits(node, edge) -> str:
    bits: List[str] = []
    node_type = getattr(node, "node_type", None)
    if node_type:
        bits.append(f"type: {node_type}")
    valid_during = getattr(node, "valid_during", None)
    if valid_during:
        bits.append(f"valid_during: {valid_during}")
    # start_date_prose is the LLM-extracted natural-language anchor when
    # start_date itself couldn't be resolved to a concrete date (e.g.
    # "after moving to Irvine"). Surfacing it tells the writer that this
    # State has a known anchor in the past — it is NOT necessarily true now.
    start_prose = getattr(node, "start_date_prose", None)
    if start_prose:
        bits.append(f"start_date_prose: {start_prose}")
    end_dt = getattr(node, "end_date", None)
    if end_dt is not None:
        end_fmt = _fmt_date(end_dt)
        if end_fmt:
            bits.append(f"end_date: {end_fmt}")
    end_prose = getattr(node, "end_date_prose", None)
    if end_prose:
        bits.append(f"end_date_prose: {end_prose}")
    date_conf = getattr(node, "start_date_confidence", None)
    if date_conf:
        bits.append(f"date_confidence: {date_conf}")
    return "; ".join(bits)


def _detect_likely_duplicates(names: Iterable[str]) -> List[tuple]:
    names = list(names)
    pairs: List[tuple] = []
    for i, a in enumerate(names):
        a_low = a.lower().strip()
        for b in names[i + 1:]:
            b_low = b.lower().strip()
            if a_low == b_low:
                continue
            if a_low in b_low or b_low in a_low:
                pairs.append((a, b))
    return pairs
