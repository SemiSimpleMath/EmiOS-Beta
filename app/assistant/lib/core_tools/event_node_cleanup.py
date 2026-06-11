"""Shared EventNode-graph cleanup for the calendar/scheduler/todo delete flows.

Consolidates the cascade-delete + node-cleanup copies found by the
2026-06-10 duplicate audit. Each tool keeps its own ``_delete_source_item``
(capabilities differ: only the calendar tool can delete google_calendar
events, only the todo tool deletes google_tasks) and passes it in as the
``delete_source_item`` callback.
"""
from __future__ import annotations

from typing import Callable, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def cleanup_event_node(source_system: str, source_id: str) -> None:
    """Remove the EventNode after deleting from its source system."""
    try:
        from app.assistant.event_graph import get_event_node_manager
        mgr = get_event_node_manager()
        node = mgr.get_node_by_source(source_system, source_id)
        if node:
            mgr.delete_node(node['node_id'], cascade=False)
    except Exception as e:
        logger.debug(f"No EventNode to clean up for {source_system}:{source_id}: {e}")


def cascade_delete_children(
    source_system: str,
    source_id: str,
    *,
    delete_source_item: Callable[[dict], Optional[str]],
    include_direct_children: bool = False,
) -> list:
    """Delete all children linked to this event in the EventNode graph.

    ``delete_source_item`` is the calling tool's source-system-specific
    deleter; it returns a "system:id" string on success, None otherwise.
    ``include_direct_children=True`` additionally walks ``children`` before
    the subtree pass (calendar's recurring-event semantics; only the first
    source of each child is attempted, matching the legacy behavior).
    """
    deleted = []
    try:
        from app.assistant.event_graph import get_event_node_manager
        mgr = get_event_node_manager()

        hierarchy = mgr.get_event_hierarchy(f"{source_system}:{source_id}")
        if not hierarchy:
            return deleted

        if include_direct_children:
            for child in hierarchy.get('children', []):
                try:
                    node_with_sources = mgr.get_node_with_sources(child['node_id'])
                    if node_with_sources:
                        for source in node_with_sources.get('sources', []):
                            child_deleted = delete_source_item(source)
                            if child_deleted:
                                deleted.append(child_deleted)
                            break  # first source only (legacy semantics)
                except Exception as e:
                    logger.error("Error deleting child: %s", e)
                    logger.debug("child delete exception", exc_info=True)

        subtree = hierarchy.get('subtree', [])
        parent_node_id = hierarchy['node']['node_id']
        for node in subtree:
            if node['node_id'] != parent_node_id:
                node_with_sources = mgr.get_node_with_sources(node['node_id'])
                if node_with_sources:
                    for source in node_with_sources.get('sources', []):
                        child_deleted = delete_source_item(source)
                        if child_deleted:
                            deleted.append(child_deleted)

    except Exception as e:
        logger.error("Error in cascade delete: %s", e)
        logger.debug("cascade delete exception", exc_info=True)
    return deleted
