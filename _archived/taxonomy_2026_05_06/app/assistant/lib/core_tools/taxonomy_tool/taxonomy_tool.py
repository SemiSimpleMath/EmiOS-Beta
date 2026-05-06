"""
Taxonomy Tool

Core tool for manipulating taxonomy structure via agent tools.

⚠️  PRODUCTION MODE - REAL DATABASE OPERATIONS ⚠️
All taxonomy operations will modify the database. Use with caution!

Operations:
- rename_category: Renames a taxonomy category
- update_description: Updates category description
- move_category: Moves category to new parent (checks for circular refs)
- merge_categories: Merges source into destination (moves children & classifications, deletes source)
- create_category: Creates a new category under a parent
- get_info: Retrieves category information
"""

from typing import Any, Dict
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

# Import taxonomy utility functions
from app.assistant.kg_core.taxonomy.utils import (
    rename_category,
    update_description,
    move_category,
    merge_categories,
    create_category,
    get_category_info
)

logger = get_logger(__name__)


class TaxonomyTool(BaseTool):
    """
    Tool for manipulating taxonomy structure.
    
    Provides agents with the ability to:
    - Rename categories
    - Update descriptions
    - Move categories (change parent)
    - Merge duplicate categories
    - Create new categories
    - Get category information
    """
    
    def __init__(self):
        super().__init__('taxonomy_tool')
        self.handlers = {
            'taxonomy_rename_category': self.handle_rename_category,
            'taxonomy_update_description': self.handle_update_description,
            'taxonomy_move_category': self.handle_move_category,
            'taxonomy_merge_categories': self.handle_merge_categories,
            'taxonomy_create_category': self.handle_create_category,
            'taxonomy_get_info': self.handle_get_info
        }
    
    def execute(self, tool_message: 'ToolMessage') -> ToolResult:
        """Execute the requested taxonomy operation."""
        logger.debug(f"Received tool_message: {tool_message}")
        
        try:
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'
            
            if not tool_name:
                return make_tool_error(
                    error_code="taxonomy_missing_tool_name",
                    message="No tool_name specified.",
                    abort_policy="abort_tool",
                    retryable=False,
                )
            
            handler = self.handlers.get(tool_name)
            
            if not handler:
                return make_tool_error(
                    error_code="taxonomy_unsupported_tool_name",
                    message=f"Unsupported tool_name '{tool_name}'.",
                    abort_policy="abort_tool",
                    retryable=False,
                )
            
            logger.debug(f"Executing handler: {tool_name} with arguments: {arguments}")
            
            tool_result = handler(arguments)
            
            return tool_result
            
        except Exception as e:
            logger.error("Error in TaxonomyTool execute(): %s", e)
            logger.debug("error in TaxonomyTool execute() exception details", exc_info=True)
            return make_tool_error(
                error_code="taxonomy_execute_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )
    
    def handle_rename_category(self, arguments: Dict[str, Any]) -> ToolResult:
        """Handle rename_category request."""
        category_id = arguments.get('category_id')
        new_label = arguments.get('new_label')
        
        if not category_id or not new_label:
            return make_tool_error(
                error_code="taxonomy_missing_required_args",
                message="Missing required arguments: category_id, new_label",
                abort_policy="abort_tool",
                retryable=False,
            )
        
        import json
        session = get_session()
        try:
            result = rename_category(session, int(category_id), new_label)
            
            if result['success']:
                return ToolResult(result_type="success", content=json.dumps(result))
            else:
                return make_tool_error(
                    error_code="taxonomy_rename_failed",
                    message=result['message'],
                    abort_policy="abort_tool",
                    retryable=False,
                )
                
        finally:
            session.close()
    
    def handle_update_description(self, arguments: Dict[str, Any]) -> ToolResult:
        """Handle update_description request."""
        category_id = arguments.get('category_id')
        new_description = arguments.get('new_description')
        
        if not category_id:
            return make_tool_error(
                error_code="taxonomy_missing_required_args",
                message="Missing required argument: category_id",
                abort_policy="abort_tool",
                retryable=False,
            )
        
        # Empty string is allowed (clears description)
        if new_description is None:
            new_description = ""
        
        import json
        session = get_session()
        try:
            result = update_description(session, int(category_id), new_description)
            
            if result['success']:
                return ToolResult(result_type="success", content=json.dumps(result))
            else:
                return make_tool_error(
                    error_code="taxonomy_update_description_failed",
                    message=result['message'],
                    abort_policy="abort_tool",
                    retryable=False,
                )
                
        finally:
            session.close()
    
    def handle_move_category(self, arguments: Dict[str, Any]) -> ToolResult:
        """Handle move_category request."""
        category_id = arguments.get('category_id')
        new_parent_id = arguments.get('new_parent_id')
        
        if not category_id:
            return make_tool_error(
                error_code="taxonomy_missing_required_args",
                message="Missing required argument: category_id",
                abort_policy="abort_tool",
                retryable=False,
            )
        
        # None or empty string means move to root
        if new_parent_id == "" or new_parent_id is None:
            new_parent_id = None
        else:
            new_parent_id = int(new_parent_id)
        
        import json
        session = get_session()
        try:
            result = move_category(session, int(category_id), new_parent_id)
            
            if result['success']:
                return ToolResult(result_type="success", content=json.dumps(result))
            else:
                return make_tool_error(
                    error_code="taxonomy_move_failed",
                    message=result['message'],
                    abort_policy="abort_tool",
                    retryable=False,
                )
                
        finally:
            session.close()
    
    def handle_merge_categories(self, arguments: Dict[str, Any]) -> ToolResult:
        """Handle merge_categories request."""
        source_id = arguments.get('source_id')
        destination_id = arguments.get('destination_id')
        
        if not source_id or not destination_id:
            return make_tool_error(
                error_code="taxonomy_missing_required_args",
                message="Missing required arguments: source_id, destination_id",
                abort_policy="abort_tool",
                retryable=False,
            )
        
        import json
        session = get_session()
        try:
            result = merge_categories(session, int(source_id), int(destination_id))
            
            if result['success']:
                return ToolResult(result_type="success", content=json.dumps(result))
            else:
                return make_tool_error(
                    error_code="taxonomy_merge_failed",
                    message=result['message'],
                    abort_policy="abort_tool",
                    retryable=False,
                )
                
        finally:
            session.close()
    
    def handle_create_category(self, arguments: Dict[str, Any]) -> ToolResult:
        """Handle create_category request."""
        parent_id = arguments.get('parent_id')
        new_label = arguments.get('new_label')
        description = arguments.get('description', '')
        
        if not parent_id or not new_label:
            return make_tool_error(
                error_code="taxonomy_missing_required_args",
                message="Missing required arguments: parent_id, new_label",
                abort_policy="abort_tool",
                retryable=False,
            )
        
        import json
        session = get_session()
        try:
            result = create_category(session, int(parent_id), new_label, description)
            
            if result['success']:
                return ToolResult(result_type="success", content=json.dumps(result))
            else:
                return make_tool_error(
                    error_code="taxonomy_create_failed",
                    message=result['message'],
                    abort_policy="abort_tool",
                    retryable=False,
                )
                
        finally:
            session.close()
    
    def handle_get_info(self, arguments: Dict[str, Any]) -> ToolResult:
        """Handle get_info request."""
        category_id = arguments.get('category_id')
        
        if not category_id:
            return make_tool_error(
                error_code="taxonomy_missing_required_args",
                message="Missing required argument: category_id",
                abort_policy="abort_tool",
                retryable=False,
            )
        
        session = get_session()
        try:
            import json
            result = get_category_info(session, int(category_id))
            
            if result['success']:
                return ToolResult(result_type="success", content=json.dumps(result))
            else:
                return make_tool_error(
                    error_code="taxonomy_get_info_failed",
                    message=result['message'],
                    abort_policy="abort_tool",
                    retryable=False,
                )
                
        finally:
            session.close()

