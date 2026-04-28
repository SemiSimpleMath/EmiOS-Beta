"""
KG Implementer Stage

Delegates KG fixes to kg_team multi-agent manager based on user responses.
Thin wrapper that creates appropriate prompts and records outcomes.
"""

import json
from typing import Dict, Any
from datetime import datetime, timezone
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
from ..data_models.problematic_node import ProblematicNode
from ..utils.node_processing_manager import NodeProcessingManager
# UserResponse and ResponseType no longer used - simplified to dict-based approach

logger = get_logger(__name__)

class KGImplementer:
    """
    Thin wrapper that delegates KG implementation to kg_team multi-agent manager.
    
    Takes output from questioner stage and creates appropriate task for kg_team,
    then records the outcome.
    """
    
    def __init__(self):
        """Initialize implementer."""
        self.multi_agent_factory = DI.multi_agent_manager_factory
        self.processing_manager = NodeProcessingManager()
        
    def implement_with_multi_tools(self, problematic_node: ProblematicNode, user_response) -> Dict[str, Any]:
        """
        Implement fixes by delegating to kg_team multi-agent manager.
        
        Args:
            problematic_node: The problematic node to fix
            user_response: Object with node_id, raw_response, and provided_data dict
            
        Returns:
            Dict containing implementation results
        """
        try:
            logger.info(f"🔧 Implementing fixes for node {problematic_node.id}")
            
            # Extract instructions from provided_data
            instructions = user_response.provided_data.get('instructions')
            
            if not instructions:
                logger.warning(f"⚠️ No instructions provided for node {problematic_node.id}")
                return {
                    "success": False,
                    "error": "No instructions provided",
                    "notes": "User response contained no actionable instructions"
                }
            
            # Delegate to kg_team for actual KG changes
            logger.info(f"📝 User provided instructions for node {problematic_node.id}, delegating to kg_team")
            logger.info(f"   Instructions: {instructions[:100]}...")
            return self._delegate_to_kg_team(problematic_node, user_response)
            
        except Exception as e:
            logger.error(f"❌ Implementation failed for node {problematic_node.id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "notes": f"Implementation error: {e}"
            }
    
    def _delegate_to_kg_team(self, node: ProblematicNode, user_response) -> Dict[str, Any]:
        """
        Delegate actual KG changes to kg_team multi-agent manager.
        
        Args:
            node: The problematic node to fix
            user_response: User's response with data
            
        Returns:
            Dict containing kg_team execution results
        """
        try:
            # Create task prompt for kg_team
            task = self._create_kg_team_task(node, user_response)
            
            logger.info(f"🤖 Creating kg_team manager for node {node.id}")
            logger.debug(f"Task for kg_team:\n{task}")
            
            # Create kg_team manager instance
            kg_team = self.multi_agent_factory.create_manager("kg_team_manager")
            
            # Create message for kg_team
            import json
            information_dict = {
                "node_id": node.id,
                "node_label": node.label,
                "problem_description": node.problem_description,
                "user_response": user_response.raw_response,
                "provided_data": user_response.provided_data,
                "source": "kg_repair_pipeline"
            }
            
            message = Message(
                data_type="agent_activation",
                sender="KG_Repair_Pipeline",
                receiver="kg_team_manager",
                content=task,
                task=task,
                information=json.dumps(information_dict),  # Convert to JSON string
                scope_context=build_system_scope_context(
                    owner_id="kg_repair_pipeline",
                    actor_id="kg_implementer",
                    surface="pipeline",
                    scope_id=f"scope::kg_repair::implementer::{node.id}",
                ),
            )
            
            # Execute via kg_team
            logger.info(f"🚀 Executing kg_team for node {node.id}")
            result = DI.manager_invoker.invoke(kg_team, message)
            
            # Parse kg_team result (request_handler returns a Message object)
            if result and hasattr(result, 'content'):
                logger.info(f"✅ kg_team completed for node {node.id}")
                logger.debug(f"kg_team result: {result.content}")
                
                # Update database status
                try:
                    self.processing_manager.update_node_status(
                        node_id=str(node.id),
                        status='completed',
                        implementation_status='success',
                        implementation_result=result.content,
                        last_action='kg_team executed successfully',
                        next_action='Node processing complete'
                    )
                    logger.info(f"📝 Updated database status for node {node.id}")
                except Exception as e:
                    logger.error(f"❌ Failed to update database status for node {node.id}: {e}")
                
                return {
                    "success": True,
                    "node_id": node.id,
                    "action": "kg_team_executed",
                    "kg_team_result": result.content,
                    "user_instruction": user_response.raw_response,
                    "notes": f"Successfully delegated to kg_team: {result.content}"
                }
            else:
                logger.warning(f"⚠️ kg_team returned unexpected result for node {node.id}: {result}")
                
                # Update database status for error
                try:
                    self.processing_manager.update_node_status(
                        node_id=str(node.id),
                        status='error',
                        implementation_status='failed',
                        implementation_result=str(result) if result else None,
                        error_details="kg_team returned no result or invalid format",
                        last_action='kg_team execution failed',
                        next_action='Review and retry'
                    )
                    logger.info(f"📝 Updated database status for node {node.id}")
                except Exception as e:
                    logger.error(f"❌ Failed to update database status for node {node.id}: {e}")
                
                return {
                    "success": False,
                    "node_id": node.id,
                    "error": "kg_team returned no result or invalid format",
                    "kg_team_result": str(result) if result else None,
                    "notes": "kg_team execution completed but result format was unexpected"
                }
                
        except Exception as e:
            logger.error(f"❌ kg_team delegation failed for node {node.id}: {e}")
            
            # Update database status for error
            try:
                self.processing_manager.update_node_status(
                    node_id=str(node.id),
                    status='error',
                    implementation_status='failed',
                    error_details=str(e),
                    last_action='kg_team delegation failed',
                    next_action='Review and retry'
                )
                logger.info(f"📝 Updated database status for node {node.id}")
            except Exception as status_e:
                logger.error(f"❌ Failed to update database status for node {node.id}: {status_e}")
            
            return {
                "success": False,
                "node_id": node.id,
                "error": str(e),
                "notes": f"kg_team delegation error: {e}"
            }
    
    def _create_kg_team_task(self, node: ProblematicNode, user_response) -> str:
        """
        Create a natural language task for kg_team based on user response.
        
        Args:
            node: The problematic node
            user_response: User's response
            
        Returns:
            Formatted task string for kg_team
        """
        # Extract instructions from user response
        instructions = user_response.provided_data.get('instructions', 'No specific instructions')
        
        task = f"""
You are tasked with implementing a fix to the knowledge graph based on user feedback.

**Context:**
A problematic node was identified in the KG and the user was asked for input.

**Problematic Node Information:**
- Node ID: {node.id}
- Label: {node.label}
- Type: {node.type or 'Unknown'}
- Description: {node.description or 'No description'}
- Problem: {node.problem_description}

**User's Instructions:**
{instructions}

**Your Task:**
Please execute the user's request to fix this node. Based on the user's natural language instruction and the provided data:

1. Interpret what the user wants to do (update, split, merge, delete, create connections, etc.)
2. Use the available KG tools to implement the changes:
   - kg_update_node: Update node attributes
   - kg_create_node: Create new nodes (if splitting or adding related nodes)
   - kg_create_edge: Create relationships between nodes
   - kg_delete_node: Delete nodes if requested
   - kg_find_node: Find related nodes if needed
   - kg_describe_node: Get details about nodes if needed

3. Make sure to:
   - Preserve important existing data unless the user explicitly says to change it
   - Create appropriate relationships if the user mentions connections
   - Use reasonable confidence scores for any new/updated data
   - Handle complex operations like splits or merges intelligently

4. Provide a clear summary of what changes were made

Please proceed with implementing the fix.
        """.strip()
        
        return task