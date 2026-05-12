"""KG neighborhood loader — shared primitive for wiki + entity-card generators.

Single entry point: ``get_entity_neighborhood(label)`` returns a typed
``EntityNeighborhood`` dataclass the caller can walk without touching the
ORM again.

The classification is driven by the A→S←B / A→Event←B patterns:
- ``Entity --rel--> State`` pairs the entity with its counterparts through
  that state (a relationship).
- ``Entity --Participant--> Event`` is an event the entity participated in,
  alongside any co-participants.
- ``State --About--> Entity`` is a belief held about the entity (we try to
  identify who holds it).
- ``Goal --Targets--> Entity`` is an active goal aimed at the entity.

Anything that doesn't fall into those buckets lands in ``misc_outgoing`` /
``misc_incoming`` so the caller can surface it without losing data.

KG gaps (missing start_dates on states/events, dangling edges, etc.) are
collected into ``kg_gaps`` rather than raised — downstream consumers decide
whether to render pages/cards with incomplete data and flag the issues for
later cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.kg.db.knowledge_graph_db import Edge, Node, get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ---------- dataclasses ----------


@dataclass
class StateConnection:
    """Entity ↔ State connection, plus any other entities connected to the same State.

    Direction:
      - outbound: ``edge_out`` set, edge runs Entity → State (e.g. Diana --has_state--> Marriage)
      - inbound:  ``edge_in`` set, edge runs State → Entity (e.g. Education-State --at_institution--> University of Foo)

    Exactly one of ``edge_out`` / ``edge_in`` is populated. ``edge`` returns
    whichever one is set so consumers that don't care about direction can
    just read ``conn.edge``.
    """
    state: Node
    edge_out: Optional[Edge] = None
    edge_in: Optional[Edge] = None
    counterparts: List[Tuple[Node, Edge]] = field(default_factory=list)

    @property
    def edge(self) -> Edge:
        e = self.edge_out or self.edge_in
        if e is None:
            raise ValueError("StateConnection has neither edge_out nor edge_in")
        return e

    @property
    def is_inbound(self) -> bool:
        return self.edge_in is not None and self.edge_out is None


@dataclass
class EventConnection:
    """Entity ↔ Event connection. Same direction conventions as StateConnection."""
    event: Node
    edge_out: Optional[Edge] = None
    edge_in: Optional[Edge] = None
    co_participants: List[Tuple[Node, Edge]] = field(default_factory=list)

    @property
    def edge(self) -> Edge:
        e = self.edge_out or self.edge_in
        if e is None:
            raise ValueError("EventConnection has neither edge_out nor edge_in")
        return e

    @property
    def is_inbound(self) -> bool:
        return self.edge_in is not None and self.edge_out is None


@dataclass
class BeliefAbout:
    """Something asserted about the entity. The believer (if known) is the Entity that
    has an edge to the same belief State.
    """
    state: Node
    edge_in: Edge  # state --About--> entity
    believer: Optional[Node] = None


@dataclass
class GoalTargeting:
    goal: Node
    edge_in: Edge  # goal --Targets--> entity
    owner: Optional[Node] = None


@dataclass
class KGGap:
    category: str
    message: str
    node_id: Optional[str] = None
    edge_id: Optional[str] = None


@dataclass
class PodNodeShim:
    """Node-shaped placeholder for an edge endpoint that's a pod URI.

    Pod URIs (``datapod:<kind>:<hash>``) can appear as edge endpoints but
    don't have ``kg_node_metadata`` rows (no mirror layer — pod_store is
    the sole source of truth). This shim lets downstream consumers walk
    the neighborhood without special-casing pod endpoints; they see a
    ``node_type='Pod'`` object with .id / .label / .category / etc.
    """
    id: str
    label: str
    node_type: str = "Pod"
    category: Optional[str] = None
    description: Optional[str] = None
    original_sentence: Optional[str] = None
    start_date: Any = None
    end_date: Any = None
    aliases: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)


def _resolve_endpoint_or_pod_shim(session, node_id: str):
    """Return Node, PodNodeShim, or None for an edge endpoint id.

    None signals a genuine dangling edge (endpoint id is neither in
    kg_node_metadata nor a known pod URI in pod_store).
    """
    node = session.query(Node).filter(Node.id == node_id).first()
    if node is not None:
        return node
    # Not a regular kg_node — is it a pod URI?
    from app.assistant.pod_store.pod_uri import POD_URI_RE
    if not POD_URI_RE.fullmatch(node_id or ""):
        return None
    from app.assistant.pod_store.pod_store import PodStore
    pod = PodStore().get(node_id)
    if pod is None:
        return None  # URI-shaped but pod was deleted — genuine gap
    body_excerpt = (pod.body or "")[:400] if pod.body else None
    return PodNodeShim(
        id=node_id,
        label=(pod.one_liner or node_id)[:200],
        category=pod.kind,
        description=pod.one_liner,
        original_sentence=body_excerpt,
    )


@dataclass
class EntityNeighborhood:
    entity: Node
    entity_card: Optional[Dict[str, Any]] = None
    # Outbound: Entity ↔ State / Event hubs the entity is the source of.
    relationships: List[StateConnection] = field(default_factory=list)
    events: List[EventConnection] = field(default_factory=list)
    # Inbound: State / Event hubs that point AT the entity. Critical for
    # passive entities (locations, institutions) that rarely originate edges
    # but are the target of many activities. Each carries the same counterpart
    # enrichment as outbound, so a wiki page for "University of Foo" can render
    # "site of Attending Class — [[Sam]], [[Riley]]".
    inbound_relationships: List[StateConnection] = field(default_factory=list)
    inbound_events: List[EventConnection] = field(default_factory=list)
    beliefs_about: List[BeliefAbout] = field(default_factory=list)
    goals_targeting: List[GoalTargeting] = field(default_factory=list)
    misc_outgoing: List[Tuple[Edge, Node]] = field(default_factory=list)
    misc_incoming: List[Tuple[Node, Edge]] = field(default_factory=list)
    kg_gaps: List[KGGap] = field(default_factory=list)


# ---------- main loader ----------


def get_entity_neighborhood(
    label: Optional[str] = None,
    *,
    node_id: Optional[str] = None,
) -> EntityNeighborhood:
    """Load the neighborhood for one Entity node.

    Lookup keys, in priority order:
      1. ``node_id`` — primary key, immutable. Use this when you have it
         (e.g. from a wiki page's frontmatter ``kg_node_id``). Survives
         renames and label edge cases (special chars, sanitization).
      2. ``label`` — display name, mutable. Use as a fallback when only
         a name is known (scratch scripts, ad-hoc lookups).

    At least one must be provided. If ``node_id`` is given the label is
    ignored for lookup but reported in errors.
    """
    if not node_id and not label:
        raise ValueError("get_entity_neighborhood requires node_id or label.")
    session = get_session()
    try:
        if node_id:
            entity = session.query(Node).filter(Node.id == str(node_id)).first()
            if entity is None:
                raise LookupError(f"No node with id={node_id!r}")
            if (entity.node_type or "") != "Entity":
                raise LookupError(
                    f"Node {node_id!r} has node_type={entity.node_type!r}; expected Entity."
                )
        else:
            entity = (
                session.query(Node)
                .filter(Node.label == label, Node.node_type == "Entity")
                .first()
            )
            if entity is None:
                raise LookupError(f"No Entity node with label={label!r}")

        entity_card = _load_entity_card(session, entity.id)
        outgoing = session.query(Edge).filter(Edge.source_id == entity.id).all()
        incoming = session.query(Edge).filter(Edge.target_id == entity.id).all()

        gaps: List[KGGap] = []
        relationships: List[StateConnection] = []
        events: List[EventConnection] = []
        misc_outgoing: List[Tuple[Edge, Node]] = []

        for edge in outgoing:
            target = _resolve_endpoint_or_pod_shim(session, edge.target_id)
            if target is None:
                gaps.append(KGGap("dangling_target", f"Edge {edge.id} points to missing node {edge.target_id}", edge_id=edge.id))
                continue

            if target.node_type == "State":
                counterparts = _find_counterpart_entities(session, target.id, exclude_entity_id=entity.id)
                relationships.append(StateConnection(state=target, edge_out=edge, counterparts=counterparts))
                if target.start_date is None:
                    gaps.append(KGGap("state_missing_start_date", f"State {target.label!r} has no start_date", node_id=target.id))
            elif target.node_type == "Event":
                co = _find_counterpart_entities(session, target.id, exclude_entity_id=entity.id)
                events.append(EventConnection(event=target, edge_out=edge, co_participants=co))
                if target.start_date is None:
                    gaps.append(KGGap("event_missing_start_date", f"Event {target.label!r} has no start_date", node_id=target.id))
            else:
                misc_outgoing.append((edge, target))

        beliefs_about: List[BeliefAbout] = []
        goals_targeting: List[GoalTargeting] = []
        misc_incoming: List[Tuple[Node, Edge]] = []
        inbound_relationships: List[StateConnection] = []
        inbound_events: List[EventConnection] = []

        for edge in incoming:
            source = _resolve_endpoint_or_pod_shim(session, edge.source_id)
            if source is None:
                gaps.append(KGGap("dangling_source", f"Edge {edge.id} has missing source {edge.source_id}", edge_id=edge.id))
                continue

            rel = (edge.relationship_type or "").lower()

            # Special-case: belief State pointing at this entity.
            if source.node_type == "State" and rel == "about":
                believer = _find_first_entity_connected_to(session, source.id, exclude_entity_id=entity.id)
                beliefs_about.append(BeliefAbout(state=source, edge_in=edge, believer=believer))
            # Special-case: Goal targeting this entity.
            elif source.node_type == "Goal" and rel == "targets":
                owner = _find_first_entity_connected_to(session, source.id, exclude_entity_id=entity.id)
                goals_targeting.append(GoalTargeting(goal=source, edge_in=edge, owner=owner))
            # Generic inbound State hub — same enrichment as outbound. Captures
            # patterns like Education-State --at_institution--> Broadway Arts.
            elif source.node_type == "State":
                counterparts = _find_counterpart_entities(session, source.id, exclude_entity_id=entity.id)
                inbound_relationships.append(StateConnection(state=source, edge_in=edge, counterparts=counterparts))
                if source.start_date is None:
                    gaps.append(KGGap("state_missing_start_date", f"State {source.label!r} has no start_date", node_id=source.id))
            # Generic inbound Event hub — Attending Class, Pickup, Drop-off etc.
            elif source.node_type == "Event":
                co = _find_counterpart_entities(session, source.id, exclude_entity_id=entity.id)
                inbound_events.append(EventConnection(event=source, edge_in=edge, co_participants=co))
                if source.start_date is None:
                    gaps.append(KGGap("event_missing_start_date", f"Event {source.label!r} has no start_date", node_id=source.id))
            else:
                misc_incoming.append((source, edge))

        return EntityNeighborhood(
            entity=entity,
            entity_card=entity_card,
            relationships=relationships,
            events=events,
            inbound_relationships=inbound_relationships,
            inbound_events=inbound_events,
            beliefs_about=beliefs_about,
            goals_targeting=goals_targeting,
            misc_outgoing=misc_outgoing,
            misc_incoming=misc_incoming,
            kg_gaps=gaps,
        )
    finally:
        session.close()


# ---------- helpers ----------


def _find_counterpart_entities(session, hub_node_id: str, *, exclude_entity_id: str) -> List[Tuple[Node, Edge]]:
    """For a State or Event node, find every Entity-type node connected to it
    (either incoming or outgoing), except the excluded entity."""
    out: List[Tuple[Node, Edge]] = []
    seen: set[str] = set()

    incoming_edges = session.query(Edge).filter(Edge.target_id == hub_node_id).all()
    outgoing_edges = session.query(Edge).filter(Edge.source_id == hub_node_id).all()

    for e in incoming_edges:
        other_id = e.source_id
        if other_id == exclude_entity_id or other_id in seen:
            continue
        other = session.query(Node).filter(Node.id == other_id, Node.node_type == "Entity").first()
        if other is not None:
            seen.add(other_id)
            out.append((other, e))

    for e in outgoing_edges:
        other_id = e.target_id
        if other_id == exclude_entity_id or other_id in seen:
            continue
        other = session.query(Node).filter(Node.id == other_id, Node.node_type == "Entity").first()
        if other is not None:
            seen.add(other_id)
            out.append((other, e))

    return out


def _find_first_entity_connected_to(session, hub_node_id: str, *, exclude_entity_id: str) -> Optional[Node]:
    candidates = _find_counterpart_entities(session, hub_node_id, exclude_entity_id=exclude_entity_id)
    return candidates[0][0] if candidates else None


def _load_entity_card(session, entity_node_id: str) -> Optional[Dict[str, Any]]:
    """Look up the v2 entity card for this KG node, if one exists.

    Returns a dict compatible with the legacy v1 EntityCard shape so existing
    consumers (e.g. wiki_renderer reads ``key_facts``) keep working. ``summary``
    comes from the v2 SUMMARY section's intro_text; ``key_facts`` is the flat
    list of bullets across the entity's bullet sections.
    """
    try:
        from app.assistant.entity_management.entity_card_v2 import (
            EntityCardV2, EntityCardSection, EntityCardBullet, SUMMARY,
        )
    except Exception as e:
        logger.debug("entity_card_v2 module unavailable: %s", e)
        return None
    try:
        card = (
            session.query(EntityCardV2)
            .filter(
                EntityCardV2.entity_node_id == entity_node_id,
                EntityCardV2.is_active == True,  # noqa: E712
            )
            .one_or_none()
        )
        if card is None:
            return None

        sections = (
            session.query(EntityCardSection)
            .filter(EntityCardSection.card_id == card.id)
            .order_by(EntityCardSection.position)
            .all()
        )
        summary_text: Optional[str] = None
        key_facts: list = []
        for s in sections:
            if s.section_name == SUMMARY and s.intro_text:
                summary_text = s.intro_text
                continue
            bullets = (
                session.query(EntityCardBullet)
                .filter(EntityCardBullet.section_id == s.id)
                .order_by(EntityCardBullet.position)
                .all()
            )
            for b in bullets:
                if b.bullet_text:
                    key_facts.append(b.bullet_text)

        return {
            "id": card.id,
            "entity_node_id": card.entity_node_id,
            "entity_type": card.entity_type,
            "summary": summary_text,
            "key_facts": key_facts,
        }
    except Exception as e:
        logger.warning("Failed to load v2 entity card for node %s: %s", entity_node_id, e)
        return None
