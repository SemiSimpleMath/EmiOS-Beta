# File: assistant/lib/tools/taxonomy_team_manager/taxonomy_team_manager.py

import json
from datetime import datetime
from typing import Dict, Any, Optional

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class taxonomy_team_manager(BaseTool):
    def __init__(self):
        super().__init__('taxonomy_team_manager')
        self.manager_interface = ManagerInterface('taxonomy_team_manager')
        
        # Initialize taxonomy query tracking
        self.query_log = []
        self.query_counter = 0

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Execute taxonomy team manager with comprehensive query tracking.
        """
        # Extract query information
        query_info = self._extract_query_info(tool_message)
        
        # Log the incoming query
        self._log_query_start(query_info)
        
        try:
            # Execute the manager
            result = self.manager_interface.execute(tool_message)
            
            # Log the result
            self._log_query_result(query_info, result)
            
            return result
            
        except Exception as e:
            # Log error
            self._log_query_error(query_info, str(e))
            raise

    def _extract_query_info(self, tool_message: ToolMessage) -> Dict[str, Any]:
        """
        Extract query information from the tool message.
        """
        arguments = tool_message.tool_data.get('arguments', {})
        
        return {
            'query_id': f"taxonomy_query_{self.query_counter}",
            'timestamp': datetime.now().isoformat(),
            'task': arguments.get('task', ''),
            'information': arguments.get('information', ''),
            'request_id': tool_message.request_id,
            'sender': tool_message.sender,
            'receiver': tool_message.receiver,
        }

    def _log_query_start(self, query_info: Dict[str, Any]):
        """
        Log the start of a KG query.
        """
        self.query_counter += 1
        
        log_entry = {
            'event': 'taxonomy_query_started',
            'query_id': query_info['query_id'],
            'timestamp': query_info['timestamp'],
            'task': query_info['task'],
            'information': query_info['information'],
            'request_id': query_info['request_id'],
            'sender': query_info['sender'],
        }
        
        self.query_log.append(log_entry)
        
        # Log to file
        logger.info(f"🏷️  Taxonomy Query Started: {query_info['query_id']}")
        logger.info(f"   Task: {query_info['task']}")
        
        # Also log to a dedicated taxonomy query log file
        self._write_to_taxonomy_log(log_entry)

    def _log_query_result(self, query_info: Dict[str, Any], result: ToolResult):
        """
        Log the result of a KG query.
        """
        log_entry = {
            'event': 'taxonomy_query_completed',
            'query_id': query_info['query_id'],
            'timestamp': datetime.now().isoformat(),
            'task': query_info['task'],
            'result_type': result.result_type if result else 'unknown',
            'result_content': result.content if result else '',
            'result_data': result.data if result and hasattr(result, 'data') else None,
            'request_id': query_info['request_id'],
            'success': True,
        }
        
        self.query_log.append(log_entry)
        
        # Log to console
        logger.info(f"✅ Taxonomy Query Completed: {query_info['query_id']}")
        logger.info(f"   Result Type: {result.result_type if result else 'unknown'}")
        logger.info(f"   Content Length: {len(result.content) if result and result.content else 0} chars")
        
        # Write to taxonomy log
        self._write_to_taxonomy_log(log_entry)

    def _log_query_error(self, query_info: Dict[str, Any], error_message: str):
        """
        Log an error in KG query execution.
        """
        log_entry = {
            'event': 'taxonomy_query_error',
            'query_id': query_info['query_id'],
            'timestamp': datetime.now().isoformat(),
            'task': query_info['task'],
            'error_message': error_message,
            'request_id': query_info['request_id'],
            'success': False,
        }
        
        self.query_log.append(log_entry)
        
        # Log to console
        logger.error(f"❌ Taxonomy Query Error: {query_info['query_id']}")
        logger.error(f"   Error: {error_message}")
        
        # Write to taxonomy log
        self._write_to_taxonomy_log(log_entry)

    def _write_to_taxonomy_log(self, log_entry: Dict[str, Any]):
        """
        Write query log entry to a dedicated taxonomy query log file.
        """
        try:
            import os
            
            # Create logs directory if it doesn't exist
            log_dir = "app/assistant/logs"
            os.makedirs(log_dir, exist_ok=True)
            
            log_file = os.path.join(log_dir, "taxonomy_query_log.jsonl")
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
                
        except Exception as e:
            logger.error(f"Failed to write to taxonomy log file: {e}")

    def get_query_statistics(self) -> Dict[str, Any]:
        """
        Get statistics about taxonomy queries.
        """
        if not self.query_log:
            return {
                'total_queries': 0,
                'successful_queries': 0,
                'failed_queries': 0,
                'success_rate': 0.0,
                'recent_queries': []
            }
        
        total_queries = len([q for q in self.query_log if q['event'] == 'taxonomy_query_started'])
        successful_queries = len([q for q in self.query_log if q['event'] == 'taxonomy_query_completed'])
        failed_queries = len([q for q in self.query_log if q['event'] == 'taxonomy_query_error'])
        
        success_rate = (successful_queries / total_queries * 100) if total_queries > 0 else 0
        
        # Get recent queries (last 10)
        recent_queries = []
        for entry in self.query_log[-20:]:  # Get last 20 entries
            if entry['event'] == 'taxonomy_query_completed':
                recent_queries.append({
                    'query_id': entry['query_id'],
                    'timestamp': entry['timestamp'],
                    'task': entry['task'],
                    'result_type': entry['result_type'],
                    'content_preview': entry['result_content'][:100] + '...' if len(entry['result_content']) > 100 else entry['result_content']
                })
        
        return {
            'total_queries': total_queries,
            'successful_queries': successful_queries,
            'failed_queries': failed_queries,
            'success_rate': round(success_rate, 2),
            'recent_queries': recent_queries[-10:],  # Last 10 successful queries
            'query_log_file': 'app/assistant/logs/taxonomy_query_log.jsonl'
        }

    def get_query_log(self, limit: Optional[int] = None) -> list:
        """
        Get the query log entries.
        
        Args:
            limit: Maximum number of entries to return (None for all)
            
        Returns:
            List of query log entries
        """
        if limit is None:
            return self.query_log.copy()
        else:
            return self.query_log[-limit:]


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return taxonomy_team_manager