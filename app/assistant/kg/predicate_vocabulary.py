"""
Canonical predicate vocabulary + normalization for KG edges.

Purpose: write-time prevention of predicate fragmentation. Before an edge
is persisted, its ``relationship_type`` runs through :func:`normalize_predicate`
which snaps obvious variants to a canonical form. Novel predicates are
logged (not rejected) so we don't silently lose genuinely new semantics.

Design:
- ``CANONICAL_PREDICATES`` is a small fixed vocabulary (~80 items) that
  covers the common case for a personal knowledge graph.
- ``PREDICATE_ALIASES`` maps case/whitespace/suffix variants to canonical.
- Unknown predicates pass through with only case+whitespace normalization
  and are logged at WARNING level so the vocabulary can be expanded later.
"""
from __future__ import annotations

import re
from typing import Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ~80 canonical predicates covering common semantics. Keep this small and
# stable. Add to it deliberately, with examples of what maps in.
CANONICAL_PREDICATES: frozenset[str] = frozenset({
    # State / attribute
    "has_state", "has_attribute", "has_property", "has_value",
    # Structure / taxonomy
    "is_a", "part_of", "contains", "includes", "member_of",
    # Semantic relations
    "about", "topic", "refers_to", "mentions", "involves", "relates_to",
    # Participation
    "participant", "attended", "witnessed",
    # Ownership
    "owner", "owned_by", "possesses",
    # Location / spatial
    "location", "located_in", "lives_at", "works_at",
    # Time / temporal
    "occurred_at", "starts_at", "ends_at", "during",
    # Goals / intentions
    "has_goal", "targets", "desires", "plans", "wants",
    # Communication
    "sent_to", "received_from", "recipient", "sender",
    # Creation / authorship
    "created_by", "creator", "author", "authored",
    # Affect / preference
    "likes", "dislikes", "prefers", "avoids",
    # Consequence
    "results_in", "caused_by", "causes", "triggers",
    # Assistance
    "helps", "assists", "blocks", "hinders",
    # Family (preserve directionality)
    "is_parent_in", "is_child_in", "is_spouse_in", "is_sibling_in",
    "is_grandparent_in", "is_grandchild_in",
    # Work
    "works_for", "employed_by", "colleague_of", "reports_to",
    # Meta / provenance
    "source", "evidence_for", "example_of", "references",
    # Common nav/IO
    "viewer", "uses", "used_by", "applies_to",
    "destination", "origin", "object", "subject",
    "has_nationality", "in_country",
    # Responsibility
    "is_responsible_for", "responsible_for",
    # Miscellany
    "content", "results_in", "involves", "addressee",
})


# Explicit alias map. Keys are RAW forms we've seen in production.
# Values must be in CANONICAL_PREDICATES. Keep the map small — it's for the
# obvious collapses, not for solving long-tail classification.
PREDICATE_ALIASES: dict[str, str] = {
    # has_state / has_goal case+separator variants
    "Has_State": "has_state", "Has State": "has_state",
    "Has_Goal": "has_goal", "Has Goal": "has_goal",
    # "about" family (14 variants collapsed to 1)
    "About": "about", "Is_About": "about", "Is_about": "about",
    "About_Entity": "about", "About_Person": "about", "About_Event": "about",
    "About_State": "about", "About_Goal": "about", "About_Location": "about",
    "About_Media": "about", "About_Concept": "about",
    "Asked_About": "about", "Asks_About": "about",
    "Platform_Asked_About": "about", "Belief_About": "about",
    # targets family
    "Target": "targets", "Targets": "targets",
    "Targets_Country": "targets", "Targets_Device": "targets",
    "Targets_Food": "targets", "Targets_Goal": "targets",
    "Targets_State": "targets", "Targets_Workspace_Type": "targets",
    # participant family
    "Participant": "participant", "Participants": "participant",
    "Part": "participant", "Defines_Participant": "participant",
    "Has_Participants_List": "participant",
    # location family
    "Location": "location", "In_Location": "location",
    "At_Location": "location", "Is_Located": "location",
    "Located": "location",
    # ownership
    "Owner": "owner", "Owns": "owner", "Possess": "owner",
    "Owned": "owned_by", "Owned_By": "owned_by",
    # topic family
    "Topic": "topic", "Has_Topic": "topic", "Is_Topic": "topic",
    # likes/preference family
    "Likes": "likes", "Like": "likes", "Enjoys": "likes",
    "Enjoyed": "likes", "Favorite": "likes", "Favorites": "likes",
    # viewer family
    "Viewer": "viewer", "Views": "viewer", "Watched": "viewer",
    "Viewing": "viewer",
    # common top predicates
    "Results_In": "results_in", "Content": "content",
    "Recipient": "recipient", "Is_A": "is_a", "Applies_To": "applies_to",
    "Involves": "involves", "Destination": "destination",
    "Is_Responsible_For": "is_responsible_for",
    "Responsible_For": "is_responsible_for",
    "Has_Nationality": "has_nationality", "In_Country": "in_country",
    # family relations
    "Is_Parent_In": "is_parent_in", "Is_Child_In": "is_child_in",
    "Is_Spouse_In": "is_spouse_in", "Is_Sibling_In": "is_sibling_in",
    "Is_Grandparent_In": "is_grandparent_in",
    "Is_Grandchild_In": "is_grandchild_in",
    # Family — extractor often emits short forms without the "_in" suffix.
    # Subject-of-edge interpretation: "X --child--> Y" means X is Y's child.
    "child": "is_child_in",
    "parent": "is_parent_in",
    "is_parent": "is_parent_in",
    "is_child": "is_child_in",
    "has_parent": "is_child_in",
    "is_married": "is_spouse_in",
    "is_spouse": "is_spouse_in",
    "is_sibling": "is_sibling_in",
    "is_grandparent": "is_grandparent_in",
    "is_grandchild": "is_grandchild_in",
    # Causal variants the extractor invents.
    "because_of": "caused_by",
    "triggered_by": "caused_by",
    "result_of": "caused_by",
    "resulted_in": "results_in",
    # Participation role variants — collapse to participant.
    "actor": "participant",
    "speaker": "participant",
    "opponent": "participant",
    "counterparty": "participant",
    "celebrant": "participant",
    "honoree": "participant",
    "questioner": "participant",
    "requester": "participant",
    "informer": "participant",
    "offerer": "participant",
    "service_provider": "participant",
    # Communication addressing variants.
    "addressed_to": "addressee",
    "addresses": "addressee",
    "intended_for": "recipient",
    "beneficiary": "recipient",
    # Spatial variants.
    "at_restaurant": "location",
    "in_vehicle": "location",
}


_WHITESPACE_RE = re.compile(r"\s+")


def _basic_normalize(raw: str) -> str:
    """Lowercase + whitespace-to-underscore + collapse repeated underscores."""
    if not raw:
        return raw
    x = raw.strip().lower()
    x = _WHITESPACE_RE.sub("_", x)
    x = re.sub(r"_{2,}", "_", x)
    return x


def normalize_predicate(raw: str) -> Tuple[str, bool]:
    """
    Normalize a raw relationship_type to its canonical form.

    Returns (canonical, was_mapped):
        - ``canonical``: the normalized predicate to persist.
        - ``was_mapped``: True iff ``raw`` matched a known alias OR needed
          case/whitespace normalization. False iff it was already clean.

    Unknown predicates pass through with basic normalization and are logged
    at INFO level so the vocabulary can grow deliberately.
    """
    if not raw:
        return raw, False

    # Exact alias match (case-sensitive) — covers capitalized variants.
    if raw in PREDICATE_ALIASES:
        return PREDICATE_ALIASES[raw], True

    normalized = _basic_normalize(raw)

    if normalized in CANONICAL_PREDICATES:
        return normalized, normalized != raw

    # Case-insensitive alias match by checking the full alias map lowercased.
    # Built lazily once; cached as a module-level dict on first use.
    global _LOWER_ALIAS_CACHE
    if _LOWER_ALIAS_CACHE is None:
        _LOWER_ALIAS_CACHE = {k.lower(): v for k, v in PREDICATE_ALIASES.items()}
    if normalized in _LOWER_ALIAS_CACHE:
        return _LOWER_ALIAS_CACHE[normalized], True

    # Unknown predicate — pass through normalized, log for review.
    logger.info(
        "[predicate_vocabulary] novel predicate observed: raw=%r normalized=%r",
        raw, normalized,
    )
    return normalized, normalized != raw


_LOWER_ALIAS_CACHE: dict[str, str] | None = None
