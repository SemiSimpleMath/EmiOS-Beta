"""
EventLinkTool - Link related events into hierarchies
=====================================================

This tool allows agents to establish parent-child relationships between
events from different systems (calendar, reminders, todos, goals).

Example usage by agent:
    1. Create calendar event for dentist → gets gcal_abc123
    2. Create reminders → gets sched_001, sched_002
    3. link_children_to_parent(
           parent="google_calendar:gcal_abc123",
           children=["scheduler:sched_001", "scheduler:sched_002"]
       )

After linking, operations like "cancel the dentist appointment" can
cascade to cancel related reminders automatically.
"""

from typing import Dict, Any, List
from datetime import datetime, timezone

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.event_graph import get_event_node_manager
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class EventLinkTool(BaseTool):
    """
    Tool for establishing hierarchical relationships between events.
    
    Supports:
    - link_children_to_parent: Link multiple children to a parent event
    - get_event_hierarchy: Get the full hierarchy for an event
    - unlink_event: Remove an event from its hierarchy
    - cancel_event_tree: Cancel an event and all its children
    """
    
    def __init__(self):
        super().__init__('event_link_tool')
        self.do_lazy_init = True
        self.manager = None
    
    def lazy_init(self):
        self.manager = get_event_node_manager()
        self.do_lazy_init = False
    
    def execute(self, tool_message: 'ToolMessage') -> ToolResult:
        if self.do_lazy_init:
            self.lazy_init()
        
        try:
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'
            
            if not tool_name:
                raise ValueError(
                    "Missing tool_name. Options: 'link_children_to_parent', "
                    "'get_event_hierarchy', 'unlink_event', 'cancel_event_tree'"
                )
            
            handler = getattr(self, f"handle_{tool_name}", None)
            if not handler:
                raise ValueError(f"Unknown tool: {tool_name}")
            
            return handler(arguments, tool_message)
            
        except Exception as e:
            logger.error("Error in EventLinkTool: %s", e)
            logger.debug("error in EventLinkTool exception details", exc_info=True)
            return make_tool_error(
                error_code="event_link_execute_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool_name": "event_link_tool"},
            )
    
    # =========================================================================
    # link_children_to_parent
    # =========================================================================
    
    def handle_link_children_to_parent(
        self, 
        arguments: Dict[str, Any], 
        tool_message: 'ToolMessage'
    ) -> ToolResult:
        """
        Link multiple child events to a parent event.
        
        Arguments:
            parent: str - "source_system:source_id" e.g. "google_calendar:abc123"
            children: List[str] - List of "source_system:source_id" strings
            parent_title: str (optional) - Title for the parent if creating new
            
        Source systems:
            - google_calendar: Google Calendar events
            - google_tasks: Google Tasks
            - scheduler: Internal scheduler/reminders
            
        Example:
            {
                "parent": "google_calendar:abc123xyz",
                "children": [
                    "scheduler:reminder_001",
                    "scheduler:reminder_002"
                ],
                "parent_title": "Dentist Appointment"
            }
        """
        parent = arguments.get('parent')
        children = arguments.get('children', [])
        parent_title = arguments.get('parent_title')
        
        if not parent:
            return ToolResult(
                result_type="error",
                content="Missing 'parent' argument. Format: 'source_system:source_id'"
            )
        
        if not children:
            return ToolResult(
                result_type="error", 
                content="Missing 'children' argument. Provide list of 'source_system:source_id' strings"
            )
        
        # Ensure children is a list
        if isinstance(children, str):
            children = [children]
        
        logger.info(f"Linking {len(children)} children to parent: {parent}")
        
        result = self.manager.link_children_to_parent(
            parent_source=parent,
            children_sources=children,
            parent_title=parent_title
        )
        
        if result['success']:
            summary = (
                f"Successfully linked {len(result['children_linked'])} items to parent.\n"
                f"Parent node: {result['parent_node_id']}\n"
                f"Linked: {[c['source'] for c in result['children_linked']]}"
            )
            return ToolResult(result_type="success", content=summary, data=result)
        else:
            error_msg = f"Linking completed with errors: {result['errors']}"
            return ToolResult(result_type="partial", content=error_msg, data=result)
    
    # =========================================================================
    # get_event_hierarchy
    # =========================================================================
    
    def handle_get_event_hierarchy(
        self,
        arguments: Dict[str, Any],
        tool_message: 'ToolMessage'
    ) -> ToolResult:
        """
        Get the full hierarchy for an event.
        
        Arguments:
            source: str - "source_system:source_id"
            
        Returns:
            - The event's node info
            - Its ancestors (parent chain)
            - Its children
            - Full subtree
        """
        source = arguments.get('source')
        
        if not source:
            return ToolResult(
                result_type="error",
                content="Missing 'source' argument. Format: 'source_system:source_id'"
            )
        
        hierarchy = self.manager.get_event_hierarchy(source)
        
        if not hierarchy:
            return ToolResult(
                result_type="not_found",
                content=f"No hierarchy found for {source}. Event may not be linked."
            )
        
        # Build summary
        node = hierarchy['node']
        ancestors = hierarchy['ancestors']
        children = hierarchy['children']
        
        lines = [f"Event: {node.get('title', 'Unknown')} ({node['node_id']})"]
        
        if ancestors:
            lines.append(f"Parent chain: {' → '.join([a.get('title', '?') for a in reversed(ancestors)])}")
        
        if children:
            lines.append(f"Children ({len(children)}): {', '.join([c.get('title', '?') for c in children])}")
        
        lines.append(f"Total in subtree: {len(hierarchy['subtree'])} items")
        
        return ToolResult(
            result_type="success",
            content="\n".join(lines),
            data=hierarchy
        )
    
    # =========================================================================
    # unlink_event
    # =========================================================================
    
    def handle_unlink_event(
        self,
        arguments: Dict[str, Any],
        tool_message: 'ToolMessage'
    ) -> ToolResult:
        """
        Remove an event from its hierarchy (keeps the event, just removes link).
        
        Arguments:
            source: str - "source_system:source_id"
            
        Note: This does NOT delete the event from the source system,
        it only removes it from the event graph.
        """
        source = arguments.get('source')
        
        if not source:
            return ToolResult(
                result_type="error",
                content="Missing 'source' argument"
            )
        
        try:
            source_system, source_id = source.split(':', 1)
        except ValueError:
            return ToolResult(
                result_type="error",
                content=f"Invalid source format: {source}. Use 'system:id'"
            )
        
        node = self.manager.get_node_by_source(source_system, source_id)
        
        if not node:
            return ToolResult(
                result_type="not_found",
                content=f"Event {source} is not in any hierarchy"
            )
        
        # Remove from parent (make it a root)
        success = self.manager.update_node(
            node['node_id'],
            parent_event_node_id=None,
            root_event_node_id=node['id']  # Become its own root
        )
        
        if success:
            return ToolResult(
                result_type="success",
                content=f"Unlinked {source} from its parent. It is now a standalone event."
            )
        else:
            return ToolResult(
                result_type="error",
                content=f"Failed to unlink {source}"
            )
    
    # =========================================================================
    # cancel_event_tree
    # =========================================================================
    
    def handle_cancel_event_tree(
        self,
        arguments: Dict[str, Any],
        tool_message: 'ToolMessage'
    ) -> ToolResult:
        """
        Cancel an event and all its children.
        
        Arguments:
            source: str - "source_system:source_id"
            
        This marks the events as cancelled in the event graph.
        NOTE: You still need to cancel them in the source systems
        (calendar, scheduler, etc.) separately.
        """
        source = arguments.get('source')
        
        if not source:
            return ToolResult(
                result_type="error",
                content="Missing 'source' argument"
            )
        
        try:
            source_system, source_id = source.split(':', 1)
        except ValueError:
            return ToolResult(
                result_type="error",
                content=f"Invalid source format: {source}"
            )
        
        node = self.manager.get_node_by_source(source_system, source_id)
        
        if not node:
            return ToolResult(
                result_type="not_found",
                content=f"Event {source} is not in the event graph"
            )
        
        cancelled, failed = self.manager.cancel_subtree(node['node_id'])
        
        if failed == 0:
            return ToolResult(
                result_type="success",
                content=f"Cancelled {cancelled} events in the hierarchy. "
                        f"Remember to also cancel them in their source systems."
            )
        else:
            return ToolResult(
                result_type="partial",
                content=f"Cancelled {cancelled} events, but {failed} failed. "
                        f"Check logs for details."
            )


# Singleton
_event_link_tool = None

def get_event_link_tool() -> EventLinkTool:
    global _event_link_tool
    if _event_link_tool is None:
        _event_link_tool = EventLinkTool()
    return _event_link_tool

