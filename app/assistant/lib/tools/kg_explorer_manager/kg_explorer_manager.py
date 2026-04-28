"""
KG Explorer Manager - Orchestrates relationship discovery and temporal reasoning
"""

import json
from datetime import datetime
from typing import Dict, Any, Union

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class kg_explorer_manager(BaseTool):
    def __init__(self):
        super().__init__('kg_explorer_manager')
        self.manager_interface = ManagerInterface('kg_explorer_manager')
        
        # Initialize exploration tracking
        self.exploration_log = []
        self.exploration_counter = 0

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Execute KG exploration manager for relationship discovery and temporal reasoning.
        """
        # Extract exploration information
        exploration_info = self._extract_exploration_info(tool_message)
        
        # Log the exploration start
        self._log_exploration_start(exploration_info)
        
        try:
            # Build the Message the manager consumes, then invoke via the
            # canonical DI manager_invoker path. ManagerInterface.execute handles
            # the generic case, but we need the kg-specific data fields
            # (node_id, exploration_type, parameters) preserved, so we construct
            # the Message here and invoke the manager instance directly.
            from app.assistant.ServiceLocator.service_locator import DI

            arguments = tool_message.tool_data.get('arguments', {})
            manager_message = Message(
                event_topic="task_request",
                task=arguments.get('task', ''),
                information=arguments.get('information', ''),
                data={
                    'node_id': arguments.get('node_id'),
                    'exploration_type': arguments.get('exploration_type', 'relationship_discovery'),
                    'parameters': exploration_info.get('parameters'),
                },
                request_id=tool_message.request_id,
                sender=tool_message.sender,
                receiver='kg_explorer_manager',
                scope_context=getattr(tool_message, 'scope_context', None),
            )

            manager = DI.multi_agent_manager_factory.create_manager('kg_explorer_manager')
            result = DI.manager_invoker.invoke(manager, manager_message)

            self._log_exploration_result(exploration_info, result)
            return result

        except Exception as e:
            self._log_exploration_error(exploration_info, str(e))
            raise

    def _extract_exploration_info(self, tool_message: ToolMessage) -> Dict[str, Any]:
        """
        Extract exploration information from the tool message.
        """
        arguments = tool_message.tool_data.get('arguments', {})
        
        # Parse parameters if provided
        parameters = None
        if arguments.get('parameters'):
            try:
                parameters = json.loads(arguments.get('parameters', '{}'))
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse parameters JSON: {arguments.get('parameters')}")
                parameters = {}
        
        return {
            'exploration_id': f"kg_exploration_{self.exploration_counter}",
            'timestamp': datetime.now().isoformat(),
            'task': arguments.get('task', ''),
            'information': arguments.get('information', ''),
            'node_id': arguments.get('node_id', ''),
            'exploration_type': arguments.get('exploration_type', 'relationship_discovery'),
            'parameters': parameters,
            'request_id': tool_message.request_id,
            'sender': tool_message.sender,
            'receiver': tool_message.receiver,
        }

    def _log_exploration_start(self, exploration_info: Dict[str, Any]):
        """
        Log the start of a KG exploration.
        """
        self.exploration_counter += 1
        
        log_entry = {
            'event': 'kg_exploration_started',
            'exploration_id': exploration_info['exploration_id'],
            'timestamp': exploration_info['timestamp'],
            'task': exploration_info['task'],
            'information': exploration_info['information'],
            'node_id': exploration_info['node_id'],
            'exploration_type': exploration_info['exploration_type'],
            'parameters': exploration_info['parameters'],
            'request_id': exploration_info['request_id'],
            'sender': exploration_info['sender'],
        }
        
        self.exploration_log.append(log_entry)
        
        # Log to console
        logger.info(f"🔍 KG Exploration Started: {exploration_info['exploration_id']}")
        logger.info(f"   Task: {exploration_info['task']}")
        logger.info(f"   Node ID: {exploration_info['node_id']}")
        
        # Write to exploration log
        self._write_to_exploration_log(log_entry)

    def _log_exploration_result(self, exploration_info: Dict[str, Any], result: ToolResult):
        """
        Log the result of a KG exploration.
        """
        log_entry = {
            'event': 'kg_exploration_completed',
            'exploration_id': exploration_info['exploration_id'],
            'timestamp': datetime.now().isoformat(),
            'task': exploration_info['task'],
            'node_id': exploration_info['node_id'],
            'result_type': result.result_type if result else 'unknown',
            'result_content': result.content if result else '',
            'result_data': result.data if result and hasattr(result, 'data') else None,
            'request_id': exploration_info['request_id'],
            'success': True,
        }
        
        self.exploration_log.append(log_entry)
        
        # Log to console
        logger.info(f"✅ KG Exploration Completed: {exploration_info['exploration_id']}")
        logger.info(f"   Result Type: {result.result_type if result else 'unknown'}")
        logger.info(f"   Content Length: {len(result.content) if result and result.content else 0} chars")
        
        # Write to exploration log
        self._write_to_exploration_log(log_entry)

    def _log_exploration_error(self, exploration_info: Dict[str, Any], error_message: str):
        """
        Log an error in KG exploration execution.
        """
        log_entry = {
            'event': 'kg_exploration_error',
            'exploration_id': exploration_info['exploration_id'],
            'timestamp': datetime.now().isoformat(),
            'task': exploration_info['task'],
            'node_id': exploration_info['node_id'],
            'error_message': error_message,
            'request_id': exploration_info['request_id'],
            'success': False,
        }
        
        self.exploration_log.append(log_entry)
        
        # Log to console
        logger.error(f"❌ KG Exploration Error: {exploration_info['exploration_id']}")
        logger.error(f"   Error: {error_message}")
        
        # Write to exploration log
        self._write_to_exploration_log(log_entry)

    def _write_to_exploration_log(self, log_entry: Dict[str, Any]):
        """
        Write exploration log entry to a dedicated KG exploration log file.
        """
        try:
            import os
            
            # Create logs directory if it doesn't exist
            log_dir = "app/assistant/logs"
            os.makedirs(log_dir, exist_ok=True)
            
            log_file = os.path.join(log_dir, "kg_exploration_log.jsonl")
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
                
        except Exception as e:
            logger.error(f"Failed to write to KG exploration log file: {e}")

    def get_exploration_statistics(self) -> Dict[str, Any]:
        """
        Get statistics about KG explorations.
        """
        if not self.exploration_log:
            return {
                'total_explorations': 0,
                'successful_explorations': 0,
                'failed_explorations': 0,
                'success_rate': 0.0,
                'recent_explorations': []
            }
        
        total_explorations = len([e for e in self.exploration_log if e['event'] == 'kg_exploration_started'])
        successful_explorations = len([e for e in self.exploration_log if e['event'] == 'kg_exploration_completed'])
        failed_explorations = len([e for e in self.exploration_log if e['event'] == 'kg_exploration_error'])
        
        success_rate = (successful_explorations / total_explorations * 100) if total_explorations > 0 else 0
        
        # Get recent explorations (last 10)
        recent_explorations = []
        for entry in self.exploration_log[-20:]:  # Get last 20 entries
            if entry['event'] == 'kg_exploration_completed':
                recent_explorations.append({
                    'exploration_id': entry['exploration_id'],
                    'timestamp': entry['timestamp'],
                    'task': entry['task'],
                    'node_id': entry['node_id'],
                    'result_type': entry['result_type'],
                    'content_preview': entry['result_content'][:100] + '...' if len(entry['result_content']) > 100 else entry['result_content']
                })
        
        return {
            'total_explorations': total_explorations,
            'successful_explorations': successful_explorations,
            'failed_explorations': failed_explorations,
            'success_rate': round(success_rate, 2),
            'recent_explorations': recent_explorations[-10:],  # Last 10 successful explorations
            'exploration_log_file': 'app/assistant/logs/kg_exploration_log.jsonl'
        }

    def get_exploration_log(self, limit: Union[int, None] = None) -> list:
        """
        Get the exploration log entries.
        
        Args:
            limit: Maximum number of entries to return (None for all)
            
        Returns:
            List of exploration log entries
        """
        if limit is None:
            return self.exploration_log.copy()
        else:
            return self.exploration_log[-limit:]


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return kg_explorer_manager
