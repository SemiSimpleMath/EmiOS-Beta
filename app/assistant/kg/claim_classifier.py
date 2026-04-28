"""
Classify a claim proposal into one of the promotion-relevant types.

Called when a proposal is created. The classification determines which
promotion fast-path it takes downstream:

- ``declarative_singular``: auto-promote on first observation (event occurred).
- ``durable``: auto-promote if no conflict (relationship/identity), soft-review.
- ``ephemeral``: auto-promote with TTL (state that auto-expires).
- ``inferred``: hold pending until corroborated (assistant-inferred or
  third-party claim).
- ``unknown``: classifier couldn't decide — defaults to pending review.

This is a deterministic function: same input → same output. No LLM.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# Predicates that describe a discrete past event; first-person user claim
# → declarative_singular (auto-promote on first observation).
# Names here match the CANONICAL_PREDICATES set in predicate_vocabulary.py
# (predicates arrive at classify_claim_type already normalized).
SINGULAR_EVENT_PREDICATES = frozenset({
    # Media / attendance
    "viewer",  # canonical for watched/viewing/saw(media)
    "attended", "witnessed",
    # Communication events
    "sent_to", "received_from", "recipient", "sender",
    "addressee", "mentions",
    # Temporal
    "occurred_at", "starts_at", "ends_at",
    # Causal
    "results_in", "caused_by", "causes", "triggers",
    # Creation
    "created_by", "creator", "author", "authored",
    # Participation in a discrete event.
    "participant",
    # Movement / direction (an event of going somewhere).
    "destination",
    # Event-content link (the thing watched/discussed/about).
    "content",
})


# Durable identity / relationship predicates; first-person → auto-promote
# with soft review (high-stakes).
DURABLE_RELATIONSHIP_PREDICATES = frozenset({
    "is_a", "is_parent_in", "is_child_in", "is_spouse_in", "is_sibling_in",
    "is_grandparent_in", "is_grandchild_in",
    "has_nationality", "works_for", "employed_by", "colleague_of", "works_at",
    "member_of", "part_of", "contains",
    "lives_at", "located_in", "in_country", "location",
    "owner", "owned_by",
    "likes", "dislikes", "prefers",  # preferences are durable
    "author", "created_by", "authored",
    # Long-term intent.
    "has_goal", "targets", "desires", "plans", "wants",
    # Intrinsic properties.
    "has_property",
})


# Predicates that generally indicate ephemeral states. Note: has_state is
# NOT here because its ephemerality depends on the state-node's type, not
# the predicate alone. A later, state-aware classifier will refine these.
EPHEMERAL_PREDICATES = frozenset({
    "feels", "is_doing",  # explicit mood/activity
})


USER_SPEAKER_ROLES = frozenset({"user", "USER"})
ASSISTANT_SPEAKER_ROLES = frozenset({"assistant", "ASSISTANT"})


def classify_claim_type(proposal: Dict[str, Any]) -> str:
    """Return one of CLAIM_TYPES based on the proposal's source + predicate.

    Expected keys on ``proposal`` (all optional, missing values treated as
    unknown context):
        source_type          -- setup_wizard | manual_ui | chat | email | ...
        speaker_role         -- user | assistant | system
        predicate            -- canonical predicate string
        subject_raw          -- used to check "first-person Jukka claim"

    Precedence:
        1. Direct user entry (setup_wizard, manual_ui) → durable
        2. Assistant / system sources → inferred
        3. Chat user-sourced + ephemeral predicate → ephemeral
        4. Chat user-sourced + singular event predicate → declarative_singular
        5. Chat user-sourced + durable relationship predicate → durable
        6. Everything else → unknown
    """
    source_type = (proposal.get("source_type") or "").strip().lower()
    speaker_role = (proposal.get("speaker_role") or "").strip().lower()
    predicate = (proposal.get("predicate") or "").strip().lower()

    # Direct user entry always treated as durable (user is seeding / editing
    # their own graph).
    if source_type in ("setup_wizard", "manual_ui", "import"):
        return "durable"

    # Assistant- or system-sourced claims need corroboration.
    if speaker_role in ASSISTANT_SPEAKER_ROLES or speaker_role == "system":
        return "inferred"

    # User-sourced, predicate-based classification.
    if speaker_role in USER_SPEAKER_ROLES:
        if predicate in EPHEMERAL_PREDICATES:
            return "ephemeral"
        if predicate in SINGULAR_EVENT_PREDICATES:
            return "declarative_singular"
        if predicate in DURABLE_RELATIONSHIP_PREDICATES:
            return "durable"

    # No speaker_role info or predicate outside our vocabulary — hold pending.
    return "unknown"


def promotion_path(claim_type: str) -> str:
    """Return a short label describing how this claim type promotes.

    Used by the promoter step to pick the right rule. Kept as a string so
    it's easy to log and reason about without importing enums.
    """
    if claim_type == "declarative_singular":
        return "auto_promote_first_observation"
    if claim_type == "durable":
        return "auto_promote_if_no_conflict_soft_review"
    if claim_type == "ephemeral":
        return "auto_promote_with_ttl"
    if claim_type == "inferred":
        return "hold_until_corroborated"
    return "hold_for_user_review"
