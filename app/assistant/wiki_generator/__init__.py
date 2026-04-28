"""Wiki generator — project KG entity neighborhoods into Obsidian markdown pages.

Read-only from the KG; writes markdown files into an Obsidian-compatible vault.
"""
# Re-export the neighborhood primitives from the shared kg_projection module
# so callers that still import ``app.assistant.wiki_generator`` continue to
# work. New code should import directly from ``app.assistant.kg_projection``.
from app.assistant.kg_projection import (
    EntityNeighborhood,
    StateConnection,
    EventConnection,
    BeliefAbout,
    GoalTargeting,
    KGGap,
    get_entity_neighborhood,
)

__all__ = [
    "EntityNeighborhood",
    "StateConnection",
    "EventConnection",
    "BeliefAbout",
    "GoalTargeting",
    "KGGap",
    "get_entity_neighborhood",
]
