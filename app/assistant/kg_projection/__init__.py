"""KG projection — shared primitives that turn a KG entity neighborhood into
structured intermediate forms that multiple consumers (wiki generator, entity
card generator, ...) can render on top of.

This module is deliberately consumer-agnostic: it produces typed data, not
markdown or JSON, so callers can build whatever artifact they need.

Currently provides:
- ``neighborhood``: typed classification of a KG entity's edges into
  relationships/events/beliefs/goals/misc buckets.
- ``sections``: section taxonomy loader (``SectionSpec``, ``load_taxonomy``).
- ``tag_cache``: on-disk sidecar I/O + ``bullet_key`` content hash for
  caching tagger results across runs.
- ``tagger``: per-bullet section-classification LLM with batching +
  ``group_bullets_by_section`` inverter.
- ``bullets``: deterministic KG-to-bullet rendering with provenance. The
  ``Bullet`` dataclass carries text + kind + subkind + sort_key + explicit
  source_node/edge_ids — no regex round-trip needed to get from markdown
  back to structured bullets.
- ``change_detection``: "which neighborhood nodes have updated_at > since".
"""
from app.assistant.kg_projection.bullets import (
    Bullet,
    render_bullets,
)
from app.assistant.kg_projection.change_detection import (
    find_changed_neighborhood_nodes,
)
from app.assistant.kg_projection.neighborhood import (
    BeliefAbout,
    EntityNeighborhood,
    EventConnection,
    GoalTargeting,
    KGGap,
    StateConnection,
    get_entity_neighborhood,
)
from app.assistant.kg_projection.sections import (
    SECTIONS_RESOURCE,
    SectionSpec,
    load_taxonomy,
    sections_as_prompt_list,
)
from app.assistant.kg_projection.tag_cache import (
    bullet_key,
    load_bullet_index,
    load_tags,
    save_bullet_index,
    save_tags,
)
from app.assistant.kg_projection.tagger import (
    TAGGER_BATCH_SIZE,
    group_bullets_by_section,
    tag_bullets,
)

__all__ = [
    # neighborhood
    "EntityNeighborhood",
    "StateConnection",
    "EventConnection",
    "BeliefAbout",
    "GoalTargeting",
    "KGGap",
    "get_entity_neighborhood",
    # bullets
    "Bullet",
    "render_bullets",
    # sections
    "SECTIONS_RESOURCE",
    "SectionSpec",
    "load_taxonomy",
    "sections_as_prompt_list",
    # tag cache
    "bullet_key",
    "load_bullet_index",
    "load_tags",
    "save_bullet_index",
    "save_tags",
    # tagger
    "TAGGER_BATCH_SIZE",
    "group_bullets_by_section",
    "tag_bullets",
    # change detection
    "find_changed_neighborhood_nodes",
]
