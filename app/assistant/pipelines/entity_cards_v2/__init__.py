"""Entity Cards v2 — section-tagged, bullet-incremental orchestrator.

Public entry points:
  build_card(entity_node_id) — full rebuild for one entity
  refresh_card_for_changed_node(node_id) — incremental refresh of bullets
                                            sourced from node_id
  rebuild_all_group_a() — bulk rebuild for every Group A entity
"""
from .builder import build_card, refresh_card_for_changed_node, rebuild_all_group_a

__all__ = ['build_card', 'refresh_card_for_changed_node', 'rebuild_all_group_a']
