"""
EventNodeManager - CRUD, Tree Operations, and Cascades
========================================================

Manages the event graph:
- Create/read/update/delete nodes
- Tree traversal (get children, get subtree, get ancestors)
- Cascade operations (cancel, reschedule)
- Source system linking
- Orphan detection
"""

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from app.models.base import get_session
from app.assistant.event_graph.event_node import (
    EventNode, EventNodeSource,
    NodeKind, DesiredState, CascadeStatus,
    initialize_event_graph_db
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class EventNodeManager:
    """
    Manages the EventNode graph.
    
    Provides:
    - CRUD operations for nodes and sources
    - Tree traversal methods
    - Cascade operations (cancel, reschedule)
    - Sync and orphan handling
    """
    
    def __init__(self):
        try:
            initialize_event_graph_db()
        except Exception as e:
            logger.warning(f"Could not initialize event graph tables: {e}")
    
    # =========================================================================
    # Node CRUD
    # =========================================================================
    
    def create_node(
        self,
        title: str,
        node_kind: NodeKind = NodeKind.SUB_EVENT,
        parent_node_id: str = None,
        start_time: datetime = None,
        end_time: datetime = None,
        offset_from_parent_minutes: int = None,
        offset_anchor: str = 'start',
        description: str = None,
        created_by: str = None,
        metadata: Dict = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new EventNode.
        
        Args:
            title: Node title
            node_kind: Type of node (root_event, sub_event, reminder, goal, etc.)
            parent_node_id: Parent's node_id (None for root nodes)
            start_time: Absolute start time
            end_time: Absolute end time
            offset_from_parent_minutes: Relative offset from parent (for cascades)
            offset_anchor: 'start' or 'end' - which parent time to offset from
            description: Optional description
            created_by: Who created this node
            metadata: Additional metadata dict
            
        Returns:
            Created node as dict, or None on error
        """
        if created_by is None:
            from app.assistant.utils.assistant_name import get_assistant_name
            created_by = get_assistant_name().lower()

        session = get_session()
        try:
            node_id = f"node_{uuid.uuid4().hex[:12]}"
            
            # Resolve parent
            parent_id = None
            root_id = None
            
            if parent_node_id:
                parent = session.query(EventNode).filter(
                    EventNode.node_id == parent_node_id
                ).first()
                
                if not parent:
                    logger.error(f"Parent node not found: {parent_node_id}")
                    return None
                
                parent_id = parent.id
                root_id = parent.root_event_node_id or parent.id
            
            node = EventNode(
                node_id=node_id,
                parent_event_node_id=parent_id,
                root_event_node_id=root_id,  # Will update after insert for root nodes
                node_kind=node_kind.value if isinstance(node_kind, NodeKind) else node_kind,
                title=title,
                description=description,
                start_time=start_time,
                end_time=end_time,
                offset_from_parent_minutes=offset_from_parent_minutes,
                offset_anchor=offset_anchor,
                desired_state=DesiredState.ACTIVE.value,
                cascade_status=CascadeStatus.NONE.value,
                created_by=created_by,
                metadata_json=metadata or {}
            )
            
            session.add(node)
            session.flush()  # Get the ID
            
            # For root nodes, set root_event_node_id to self
            if parent_id is None:
                node.root_event_node_id = node.id
            
            session.commit()
            
            result = node.to_dict()
            logger.info(f"🌳 Created node: {node_id} ({node_kind}) - '{title}'")
            return result
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Error creating node: {e}")
            return None
        finally:
            session.close()
    
    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get a node by its node_id."""
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            return node.to_dict() if node else None
        finally:
            session.close()
    
    def get_node_with_sources(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get a node with its source links."""
        session = get_session()
        try:
            node = session.query(EventNode).options(
                joinedload(EventNode.sources)
            ).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return None
            
            result = node.to_dict()
            result['sources'] = [s.to_dict() for s in node.sources]
            return result
        finally:
            session.close()
    
    def update_node(self, node_id: str, **kwargs) -> bool:
        """Update node fields."""
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return False
            
            for key, value in kwargs.items():
                if hasattr(node, key):
                    setattr(node, key, value)
            
            session.commit()
            logger.info(f"🌳 Updated node: {node_id}")
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Error updating node {node_id}: {e}")
            return False
        finally:
            session.close()
    
    def delete_node(self, node_id: str, cascade: bool = True) -> bool:
        """
        Delete a node.
        
        Args:
            node_id: Node to delete
            cascade: If True, also delete all descendants
        """
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return False
            
            if cascade:
                # Delete all descendants first
                descendants = self._get_all_descendants_internal(session, node.id)
                for desc in descendants:
                    # Delete sources first
                    session.query(EventNodeSource).filter(
                        EventNodeSource.event_node_id == desc.id
                    ).delete()
                    session.delete(desc)
            
            # Delete this node's sources
            session.query(EventNodeSource).filter(
                EventNodeSource.event_node_id == node.id
            ).delete()
            
            session.delete(node)
            session.commit()
            
            logger.info(f"🌳 Deleted node: {node_id} (cascade={cascade})")
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Error deleting node {node_id}: {e}")
            return False
        finally:
            session.close()
    
    # =========================================================================
    # Source CRUD
    # =========================================================================
    
    def add_source(
        self,
        node_id: str,
        source_system: str,
        source_type: str,
        source_id: str,
        is_primary: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Link a source system record to a node.
        
        Args:
            node_id: The EventNode's node_id
            source_system: 'google_calendar', 'google_tasks', 'scheduler', 'internal'
            source_type: 'calendar', 'todo', 'reminder', 'goal'
            source_id: ID in the source system
            is_primary: Whether this is the primary source
        """
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                logger.error(f"Node not found: {node_id}")
                return None
            
            source = EventNodeSource(
                event_node_id=node.id,
                source_system=source_system,
                source_type=source_type,
                source_id=source_id,
                is_primary=is_primary,
                last_seen_at=datetime.now(timezone.utc),
                sync_status='synced'
            )
            
            session.add(source)
            session.commit()
            
            result = source.to_dict()
            logger.info(f"🔗 Linked source to {node_id}: {source_system}/{source_id}")
            return result
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Error adding source: {e}")
            return None
        finally:
            session.close()
    
    def get_node_by_source(self, source_system: str, source_id: str) -> Optional[Dict[str, Any]]:
        """Find a node by its source system reference."""
        session = get_session()
        try:
            source = session.query(EventNodeSource).filter(
                EventNodeSource.source_system == source_system,
                EventNodeSource.source_id == source_id
            ).first()
            
            if not source:
                return None
            
            node = session.query(EventNode).filter(
                EventNode.id == source.event_node_id
            ).first()
            
            return node.to_dict() if node else None
        finally:
            session.close()
    
    # =========================================================================
    # Tree Traversal
    # =========================================================================
    
    def get_children(self, node_id: str) -> List[Dict[str, Any]]:
        """Get direct children of a node."""
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return []
            
            children = session.query(EventNode).filter(
                EventNode.parent_event_node_id == node.id
            ).all()
            
            return [c.to_dict() for c in children]
        finally:
            session.close()
    
    def get_subtree(self, node_id: str) -> List[Dict[str, Any]]:
        """Get all descendants of a node (including the node itself)."""
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return []
            
            # Get all nodes with this root
            if node.is_root():
                subtree = session.query(EventNode).filter(
                    EventNode.root_event_node_id == node.id
                ).all()
            else:
                # For non-root, we need recursive traversal
                subtree = [node] + self._get_all_descendants_internal(session, node.id)
            
            return [n.to_dict() for n in subtree]
        finally:
            session.close()
    
    def _get_all_descendants_internal(self, session, node_id: int) -> List[EventNode]:
        """Internal recursive method to get all descendants."""
        children = session.query(EventNode).filter(
            EventNode.parent_event_node_id == node_id
        ).all()
        
        descendants = list(children)
        for child in children:
            descendants.extend(self._get_all_descendants_internal(session, child.id))
        
        return descendants
    
    def get_ancestors(self, node_id: str) -> List[Dict[str, Any]]:
        """Get all ancestors of a node (parent, grandparent, etc.)."""
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return []
            
            ancestors = []
            current = node
            
            while current.parent_event_node_id is not None:
                parent = session.query(EventNode).filter(
                    EventNode.id == current.parent_event_node_id
                ).first()
                if parent:
                    ancestors.append(parent.to_dict())
                    current = parent
                else:
                    break
            
            return ancestors
        finally:
            session.close()
    
    def get_root(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get the root node for any node in the tree."""
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return None
            
            if node.root_event_node_id:
                root = session.query(EventNode).filter(
                    EventNode.id == node.root_event_node_id
                ).first()
                return root.to_dict() if root else None
            
            return node.to_dict()
        finally:
            session.close()
    
    # =========================================================================
    # Cascade Operations
    # =========================================================================
    
    def cancel_subtree(self, node_id: str) -> Tuple[int, int]:
        """
        Cancel a node and all its descendants.
        
        Sets desired_state = 'cancelled' and propagates to source systems.
        
        Returns:
            Tuple of (nodes_cancelled, nodes_failed)
        """
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return (0, 0)
            
            # Mark cascade as in progress
            node.cascade_status = CascadeStatus.IN_PROGRESS.value
            session.commit()
            
            # Get all descendants
            descendants = self._get_all_descendants_internal(session, node.id)
            all_nodes = [node] + descendants
            
            cancelled = 0
            failed = 0
            
            for n in all_nodes:
                try:
                    n.desired_state = DesiredState.CANCELLED.value
                    n.cascade_status = CascadeStatus.COMPLETED.value
                    
                    # TODO: Actually cancel in source systems
                    # self._cancel_in_sources(session, n)
                    
                    cancelled += 1
                except Exception as e:
                    n.cascade_status = CascadeStatus.FAILED.value
                    n.cascade_error = str(e)
                    failed += 1
            
            # Update root cascade status
            if failed > 0:
                node.cascade_status = CascadeStatus.PARTIAL.value
            else:
                node.cascade_status = CascadeStatus.COMPLETED.value
            
            session.commit()
            
            logger.info(f"🌳 Cancelled subtree {node_id}: {cancelled} cancelled, {failed} failed")
            return (cancelled, failed)
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Error cancelling subtree {node_id}: {e}")
            return (0, 0)
        finally:
            session.close()
    
    def reschedule_subtree(self, node_id: str, new_start: datetime) -> Tuple[int, int]:
        """
        Reschedule a node and cascade the time shift to descendants.
        
        Children with offset_from_parent will have their absolute times
        recalculated based on the new parent time.
        
        Returns:
            Tuple of (nodes_rescheduled, nodes_failed)
        """
        session = get_session()
        try:
            node = session.query(EventNode).filter(
                EventNode.node_id == node_id
            ).first()
            
            if not node:
                return (0, 0)
            
            old_start = node.start_time
            if not old_start:
                logger.warning(f"Node {node_id} has no start_time, cannot compute delta")
                return (0, 0)
            
            # Ensure timezone awareness
            if old_start.tzinfo is None:
                old_start = old_start.replace(tzinfo=timezone.utc)
            
            delta = new_start - old_start
            
            # Mark cascade as in progress
            node.cascade_status = CascadeStatus.IN_PROGRESS.value
            session.commit()
            
            rescheduled = 0
            failed = 0
            
            # Update this node
            node.start_time = new_start
            if node.end_time:
                end = node.end_time
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                node.end_time = end + delta
            node.desired_state = DesiredState.RESCHEDULED.value
            rescheduled += 1
            
            # Cascade to descendants
            descendants = self._get_all_descendants_internal(session, node.id)
            
            for desc in descendants:
                try:
                    if desc.offset_from_parent_minutes is not None:
                        # Recalculate from parent's new time
                        parent = session.query(EventNode).filter(
                            EventNode.id == desc.parent_event_node_id
                        ).first()
                        
                        if parent:
                            anchor_time = parent.start_time if desc.offset_anchor == 'start' else parent.end_time
                            if anchor_time:
                                # Ensure timezone awareness
                                if anchor_time.tzinfo is None:
                                    anchor_time = anchor_time.replace(tzinfo=timezone.utc)
                                desc.start_time = anchor_time + timedelta(minutes=desc.offset_from_parent_minutes)
                    else:
                        # Just shift by delta
                        if desc.start_time:
                            start = desc.start_time
                            if start.tzinfo is None:
                                start = start.replace(tzinfo=timezone.utc)
                            desc.start_time = start + delta
                        if desc.end_time:
                            end = desc.end_time
                            if end.tzinfo is None:
                                end = end.replace(tzinfo=timezone.utc)
                            desc.end_time = end + delta
                    
                    desc.desired_state = DesiredState.RESCHEDULED.value
                    desc.cascade_status = CascadeStatus.COMPLETED.value
                    
                    # TODO: Actually update in source systems
                    # self._update_in_sources(session, desc)
                    
                    rescheduled += 1
                    
                except Exception as e:
                    desc.cascade_status = CascadeStatus.FAILED.value
                    desc.cascade_error = str(e)
                    failed += 1
            
            # Update root cascade status
            if failed > 0:
                node.cascade_status = CascadeStatus.PARTIAL.value
            else:
                node.cascade_status = CascadeStatus.COMPLETED.value
            
            session.commit()
            
            logger.info(f"🌳 Rescheduled subtree {node_id}: {rescheduled} updated, {failed} failed")
            return (rescheduled, failed)
            
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Error rescheduling subtree {node_id}: {e}")
            return (0, 0)
        finally:
            session.close()
    
    # =========================================================================
    # Query Helpers
    # =========================================================================
    
    def get_root_nodes(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all root-level nodes."""
        session = get_session()
        try:
            roots = session.query(EventNode).filter(
                EventNode.parent_event_node_id == None
            ).order_by(EventNode.created_at.desc()).limit(limit).all()
            
            return [r.to_dict() for r in roots]
        finally:
            session.close()
    
    def get_orphaned_nodes(self) -> List[Dict[str, Any]]:
        """Get nodes marked as orphaned."""
        session = get_session()
        try:
            orphans = session.query(EventNode).filter(
                EventNode.orphaned == True
            ).all()
            
            return [o.to_dict() for o in orphans]
        finally:
            session.close()
    
    def get_pending_cascades(self) -> List[Dict[str, Any]]:
        """Get nodes with pending or failed cascade operations."""
        session = get_session()
        try:
            pending = session.query(EventNode).filter(
                EventNode.cascade_status.in_([
                    CascadeStatus.PENDING.value,
                    CascadeStatus.IN_PROGRESS.value,
                    CascadeStatus.FAILED.value,
                    CascadeStatus.PARTIAL.value
                ])
            ).all()
            
            return [p.to_dict() for p in pending]
        finally:
            session.close()
    
    # =========================================================================
    # Linking Helpers (for tool integration)
    # =========================================================================
    
    def find_or_create_node_for_source(
        self,
        source_system: str,
        source_type: str,
        source_id: str,
        title: str = None,
        start_time: datetime = None,
        end_time: datetime = None,
        node_kind: NodeKind = NodeKind.ROOT_EVENT
    ) -> Optional[Dict[str, Any]]:
        """
        Find an existing EventNode linked to this source, or create one.
        
        This is used to "promote" a standalone event (e.g., calendar event)
        to an EventNode when it needs to participate in a hierarchy.
        
        Args:
            source_system: 'google_calendar', 'google_tasks', 'scheduler'
            source_type: 'calendar', 'todo', 'reminder'
            source_id: ID in the source system
            title: Event title (used if creating new node)
            start_time: Event start time
            end_time: Event end time
            node_kind: Type of node to create if needed
            
        Returns:
            EventNode dict (existing or newly created)
        """
        # First, check if a node already exists for this source
        existing = self.get_node_by_source(source_system, source_id)
        if existing:
            logger.info(f"🔗 Found existing node for {source_system}:{source_id}")
            return existing
        
        # Create new node
        node = self.create_node(
            title=title or f"Event from {source_system}",
            node_kind=node_kind,
            start_time=start_time,
            end_time=end_time,
            created_by='link_helper'
        )
        
        if not node:
            return None
        
        # Link to source
        self.add_source(
            node_id=node['node_id'],
            source_system=source_system,
            source_type=source_type,
            source_id=source_id,
            is_primary=True
        )
        
        logger.info(f"🌳 Promoted {source_system}:{source_id} to EventNode {node['node_id']}")
        return node
    
    def link_children_to_parent(
        self,
        parent_source: str,
        children_sources: List[str],
        parent_title: str = None,
        parent_start_time: datetime = None,
        parent_end_time: datetime = None
    ) -> Dict[str, Any]:
        """
        Link multiple children to a parent event.
        
        This is the main tool for agents to establish event hierarchies.
        
        Args:
            parent_source: "source_system:source_id" e.g. "google_calendar:abc123"
            children_sources: List of "source_system:source_id" strings
            parent_title: Title for parent (if needs to be created)
            parent_start_time: Start time for parent
            parent_end_time: End time for parent
            
        Returns:
            Dict with parent_node_id, children_linked, errors
        """
        result = {
            'success': False,
            'parent_node_id': None,
            'children_linked': [],
            'errors': []
        }
        
        # Parse parent source
        try:
            parent_system, parent_id = parent_source.split(':', 1)
        except ValueError:
            result['errors'].append(f"Invalid parent format: {parent_source}. Use 'system:id'")
            return result
        
        # Determine source type from system
        source_type_map = {
            'google_calendar': 'calendar',
            'google_tasks': 'todo',
            'scheduler': 'reminder',
            'internal': 'internal'
        }
        parent_source_type = source_type_map.get(parent_system, 'calendar')
        
        # Find or create parent node
        parent_node = self.find_or_create_node_for_source(
            source_system=parent_system,
            source_type=parent_source_type,
            source_id=parent_id,
            title=parent_title,
            start_time=parent_start_time,
            end_time=parent_end_time,
            node_kind=NodeKind.ROOT_EVENT
        )
        
        if not parent_node:
            result['errors'].append(f"Failed to create parent node for {parent_source}")
            return result
        
        result['parent_node_id'] = parent_node['node_id']
        
        # Link each child
        for child_source in children_sources:
            try:
                child_system, child_id = child_source.split(':', 1)
            except ValueError:
                result['errors'].append(f"Invalid child format: {child_source}")
                continue
            
            child_source_type = source_type_map.get(child_system, 'reminder')
            
            # Determine child node kind based on source
            child_kind_map = {
                'google_calendar': NodeKind.SUB_EVENT,
                'google_tasks': NodeKind.TASK,
                'scheduler': NodeKind.REMINDER
            }
            child_kind = child_kind_map.get(child_system, NodeKind.REMINDER)
            
            # Check if child already has a node
            existing_child = self.get_node_by_source(child_system, child_id)
            
            if existing_child:
                # Update existing node to point to parent
                self.update_node(
                    existing_child['node_id'],
                    parent_event_node_id=parent_node['id'],
                    root_event_node_id=parent_node.get('root_event_node_id') or parent_node['id']
                )
                result['children_linked'].append({
                    'source': child_source,
                    'node_id': existing_child['node_id'],
                    'action': 'updated'
                })
            else:
                # Create new child node
                child_node = self.create_node(
                    title=f"Linked from {child_system}",
                    node_kind=child_kind,
                    parent_node_id=parent_node['node_id'],
                    created_by='link_helper'
                )
                
                if child_node:
                    self.add_source(
                        node_id=child_node['node_id'],
                        source_system=child_system,
                        source_type=child_source_type,
                        source_id=child_id,
                        is_primary=True
                    )
                    result['children_linked'].append({
                        'source': child_source,
                        'node_id': child_node['node_id'],
                        'action': 'created'
                    })
                else:
                    result['errors'].append(f"Failed to create child node for {child_source}")
        
        result['success'] = len(result['errors']) == 0
        
        logger.info(
            f"🔗 Linked {len(result['children_linked'])} children to parent {parent_node['node_id']} "
            f"({len(result['errors'])} errors)"
        )
        
        return result
    
    def get_event_hierarchy(self, source: str) -> Optional[Dict[str, Any]]:
        """
        Get the full hierarchy for an event given its source reference.
        
        Args:
            source: "source_system:source_id"
            
        Returns:
            Dict with node info, parent chain, and children
        """
        try:
            source_system, source_id = source.split(':', 1)
        except ValueError:
            return None
        
        node = self.get_node_by_source(source_system, source_id)
        if not node:
            return None
        
        return {
            'node': node,
            'ancestors': self.get_ancestors(node['node_id']),
            'children': self.get_children(node['node_id']),
            'subtree': self.get_subtree(node['node_id'])
        }
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the event graph."""
        session = get_session()
        try:
            total = session.query(EventNode).count()
            roots = session.query(EventNode).filter(
                EventNode.parent_event_node_id == None
            ).count()
            
            by_kind = {}
            for kind in NodeKind:
                count = session.query(EventNode).filter(
                    EventNode.node_kind == kind.value
                ).count()
                by_kind[kind.value] = count
            
            orphaned = session.query(EventNode).filter(
                EventNode.orphaned == True
            ).count()
            
            sources = session.query(EventNodeSource).count()
            
            return {
                'total_nodes': total,
                'root_nodes': roots,
                'by_kind': by_kind,
                'orphaned': orphaned,
                'total_sources': sources
            }
        finally:
            session.close()


# Singleton instance
_event_node_manager: Optional[EventNodeManager] = None


def get_event_node_manager() -> EventNodeManager:
    """Get the singleton EventNodeManager instance."""
    global _event_node_manager
    if _event_node_manager is None:
        _event_node_manager = EventNodeManager()
    return _event_node_manager

