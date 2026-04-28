"""
Event Graph - Hierarchical Event Management
============================================

Provides a semantic layer for connecting events across different systems
(Google Calendar, Scheduler, Tasks) with tree-structured relationships
and cascade operations.

Usage:
    from app.assistant.event_graph import get_event_node_manager
    
    mgr = get_event_node_manager()
    
    # Create a root event (Cruise 2025)
    cruise = mgr.create_node(
        title="Cruise 2025",
        node_kind=NodeKind.ROOT_EVENT,
        start_time=datetime(2025, 7, 1, 8, 0),
        end_time=datetime(2025, 7, 8, 18, 0)
    )
    
    # Add a child event (Packing Day)
    packing = mgr.create_node(
        title="Packing Day",
        node_kind=NodeKind.SUB_EVENT,
        parent_node_id=cruise['node_id'],
        offset_from_parent_minutes=-1440,  # 1 day before
        offset_anchor='start'
    )
    
    # Add a reminder
    reminder = mgr.create_node(
        title="Start packing clothes",
        node_kind=NodeKind.REMINDER,
        parent_node_id=packing['node_id'],
        offset_from_parent_minutes=-120,  # 2 hours before packing day
    )
    
    # Link to source systems
    mgr.add_source(cruise['node_id'], 'google_calendar', 'calendar', 'gcal_event_123')
    
    # Cancel the whole trip
    cancelled, failed = mgr.cancel_subtree(cruise['node_id'])
    
    # Or reschedule
    rescheduled, failed = mgr.reschedule_subtree(cruise['node_id'], new_start)
"""

from app.assistant.event_graph.event_node import (
    EventNode,
    EventNodeSource,
    NodeKind,
    DesiredState,
    CascadeStatus,
    initialize_event_graph_db,
    drop_event_graph_db,
    reset_event_graph_db
)

from app.assistant.event_graph.event_node_manager import (
    EventNodeManager,
    get_event_node_manager
)

__all__ = [
    'EventNode',
    'EventNodeSource',
    'NodeKind',
    'DesiredState',
    'CascadeStatus',
    'EventNodeManager',
    'get_event_node_manager',
    'initialize_event_graph_db',
    'drop_event_graph_db',
    'reset_event_graph_db',
]

