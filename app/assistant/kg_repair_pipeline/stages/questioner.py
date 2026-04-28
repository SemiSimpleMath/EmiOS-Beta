"""
KG Questioner Stage

Delegates to questioner_manager multi-agent manager to ask users about problematic nodes.
Thin wrapper that creates appropriate prompts and parses user responses.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
import json
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
from ..data_models.problematic_node import ProblematicNode
from ..data_models.user_response import UserResponse, ResponseType
from ..utils.node_processing_manager import NodeProcessingManager

logger = get_logger(__name__)

class KGQuestioner:
    """
    Thin wrapper that delegates user questioning to questioner_manager multi-agent manager.
    
    Takes problematic nodes and creates appropriate task for questioner_manager,
    then parses the user's response.
    """
    
    def __init__(self):
        """Initialize questioner."""
        self.multi_agent_factory = DI.multi_agent_manager_factory
        self.processing_manager = NodeProcessingManager()
        
    def ask_user_about_node(self, node: ProblematicNode) -> Optional[UserResponse]:
        """
        Ask the user about a specific problematic node by delegating to questioner_manager.
        
        Args:
            node: The problematic node to ask about
            
        Returns:
            UserResponse object with user's response, or None if failed
        """
        try:
            logger.info(f"❓ Asking user about node {node.id}: {node.problem_description}")
            
            # Create question for the user
            question = self._create_question_for_node(node)
            
            # Create questioner_manager
            questioner_manager = self.multi_agent_factory.create_manager("questioner_manager")
            
            # Create message for manager
            # Convert information dict to JSON string (Message expects string)
            information_str = json.dumps({
                "problem_description": node.problem_description,
                "node_type": node.type,
                "label": node.label,
                "semantic_label": getattr(node, 'semantic_label', ''),
                "description": node.description,
                "category": node.category,
                "aliases": node.node_aliases or [],
                "start_date": node.start_date,
                "end_date": node.end_date,
                "valid_during": node.valid_during
            })
            
            message = Message(
                data_type="agent_activation",
                sender="KG_Repair_Pipeline",
                receiver="questioner_manager",
                content=question,
                task=question,
                information=information_str,
                scope_context=build_system_scope_context(
                    owner_id="kg_repair_pipeline",
                    actor_id="kg_questioner",
                    surface="pipeline",
                    scope_id=f"scope::kg_repair::questioner::{node.id}",
                ),
            )
            
            # Execute via manager
            result = DI.manager_invoker.invoke(questioner_manager, message)
            
            if not result:
                logger.error(f"❌ questioner_manager returned no result for node {node.id}")
                return None
                
            # Parse user response from manager result (extracts from result.data)
            user_response = self._parse_manager_response(result, node)
            
            logger.info(f"✅ User responded to node {node.id}: {user_response.response_type}")
            return user_response
            
        except Exception as e:
            logger.error(f"❌ Failed to ask user about node {node.id}: {e}")
            return None
    
    def _create_question_for_node(self, node: ProblematicNode) -> str:
        """
        Create a user-friendly question about a problematic node.
        
        Args:
            node: The problematic node to ask about
            
        Returns:
            Formatted question string
        """
        question = f"""
I found a potential issue in the knowledge graph that might need your input:

**Node ID:** {node.id}
**Problem:** {node.problem_description}

Please let me know:
- Do you have the missing information to fix this?
- Should I skip this node for now?
- Should I ask you about this later?
- Or do you have no idea about this node?

You can respond with:
- "Yes, here's the data: [provide the missing information]"
- "Skip this node"
- "Ask me again tomorrow" (or specify a time)
- "I have no idea about this"
- "This isn't actually a problem"

What would you like to do?
        """.strip()
        
        return question
    
    def _parse_manager_response(self, manager_result, node: ProblematicNode) -> Dict[str, Any]:
        """
        Parse the manager's response from the questioner agent.
        
        Manager returns a ToolResult with final_answer data containing agent's structured output.
        
        Args:
            manager_result: ToolResult from questioner_manager.request_handler()
            node: The original problematic node
            
        Returns:
            Dict with parsed response fields
        """
        try:
            # Manager returns ToolResult with data field containing final_answer from blackboard
            agent_output = manager_result.data if hasattr(manager_result, 'data') else None
            
            if not agent_output:
                logger.warning(f"⚠️ No data in manager result for node {node.id}")
                return {
                    "node_id": node.id,
                    "pause_entire_pipeline": False,
                    "skip_this_node": False,
                    "postpone_until": None,
                    "instructions": None,
                    "raw_response": manager_result.content if hasattr(manager_result, 'content') else "No response"
                }
            
            # Extract fields from agent output (final_answer from blackboard)
            return {
                "node_id": node.id,
                "pause_entire_pipeline": agent_output.get("pause_entire_pipeline", False),
                "skip_this_node": agent_output.get("skip_this_node", False),
                "postpone_until": agent_output.get("postpone_until"),
                "instructions": agent_output.get("instructions"),
                "raw_response": manager_result.content if hasattr(manager_result, 'content') else "User interaction completed"
            }
            
        except Exception as e:
            logger.error(f"❌ Failed to parse manager response for node {node.id}: {e}")
            return {
                "node_id": node.id,
                "pause_entire_pipeline": False,
                "skip_this_node": True,  # Default to skip on error
                "postpone_until": None,
                "instructions": None,
                "raw_response": f"Error parsing response: {e}"
            }
    
    def _classify_response(self, response: str) -> ResponseType:
        """
        Intelligently classify the user's natural language response into a response type.
        Uses pattern matching and context analysis to understand user intent.
        
        Args:
            response: The user's raw response text
            
        Returns:
            ResponseType enum value
        """
        response_lower = response.lower().strip()
        
        # Handle empty or very short responses
        if len(response_lower) < 3:
            return ResponseType.NO_IDEA
            
        # Check for explicit data provision patterns
        data_provision_patterns = [
            # Direct data provision
            "here's the data", "here is the data", "the data is", "the information is",
            "start_date", "end_date", "description", "confidence", "the date is",
            "it's", "it is", "the wedding was", "the event was", "happened on",
            # Data with specific values
            r"\d{4}-\d{2}-\d{2}",  # Date patterns
            r"january|february|march|april|may|june|july|august|september|october|november|december",
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday",
            # Specific data formats
            "yes,", "sure,", "okay,", "alright,", "fine,", "here you go",
            "the missing", "the date", "the description", "the confidence"
        ]
        
        for pattern in data_provision_patterns:
            if pattern in response_lower or (isinstance(pattern, str) and pattern in response_lower):
                return ResponseType.PROVIDE_DATA
                
        # Check for skip patterns
        skip_patterns = [
            "skip", "ignore", "not important", "don't care", "don't bother",
            "not relevant", "not needed", "pass", "next", "move on",
            "i don't want to", "i don't need to", "not worth it"
        ]
        
        for pattern in skip_patterns:
            if pattern in response_lower:
                return ResponseType.SKIP
                
        # Check for ask later patterns
        ask_later_patterns = [
            "ask me again", "ask later", "ask tomorrow", "ask in", "remind me",
            "later", "tomorrow", "next time", "not now", "busy", "don't have time",
            "ask me in", "remind me in", "check back", "come back"
        ]
        
        for pattern in ask_later_patterns:
            if pattern in response_lower:
                return ResponseType.ASK_LATER
                
        # Check for no idea patterns
        no_idea_patterns = [
            "no idea", "don't know", "not sure", "unfamiliar", "never heard",
            "no clue", "no information", "can't help", "don't remember",
            "i don't know", "i'm not sure", "i have no idea", "i don't remember"
        ]
        
        for pattern in no_idea_patterns:
            if pattern in response_lower:
                return ResponseType.NO_IDEA
                
        # Check for invalid problem patterns
        invalid_patterns = [
            "not a problem", "this is fine", "correct as is", "invalid",
            "this is correct", "this is right", "no problem", "it's fine",
            "this is good", "this is okay", "this is correct", "leave it",
            "don't change", "it's already", "this doesn't need"
        ]
        
        for pattern in invalid_patterns:
            if pattern in response_lower:
                return ResponseType.INVALID
                
        # Check for hold off patterns
        hold_off_patterns = [
            "hold off", "wait", "pause", "stop", "don't do", "not yet",
            "hold on", "wait a minute", "not right now"
        ]
        
        for pattern in hold_off_patterns:
            if pattern in response_lower:
                # Check if it's about all nodes or just this one
                if "all" in response_lower or "everything" in response_lower:
                    return ResponseType.ASK_LATER  # Treat as ask later for now
                else:
                    return ResponseType.SKIP
                    
        # Check for data-like content (heuristic)
        if self._looks_like_data(response):
            return ResponseType.PROVIDE_DATA
            
        # Check for question patterns (might be asking for clarification)
        question_patterns = ["?", "what", "how", "when", "where", "why", "which"]
        if any(pattern in response_lower for pattern in question_patterns):
            return ResponseType.NO_IDEA  # Treat questions as no idea for now
            
        # Default to no idea for unclear responses
        return ResponseType.NO_IDEA
    
    def _looks_like_data(self, response: str) -> bool:
        """
        Heuristic to determine if response looks like it contains data.
        
        Args:
            response: The user's response text
            
        Returns:
            True if response looks like it contains data
        """
        response_lower = response.lower()
        
        # Check for data indicators
        data_indicators = [
            # Contains specific values
            any(char.isdigit() for char in response),
            # Contains date-like patterns
            any(month in response_lower for month in [
                "january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"
            ]),
            # Contains structured information
            ":" in response and len(response.split(":")) > 1,
            # Contains specific data fields
            any(field in response_lower for field in [
                "date", "time", "description", "name", "location", "place"
            ]),
            # Contains specific values or measurements
            any(word in response_lower for word in [
                "high", "medium", "low", "good", "bad", "important", "critical"
            ])
        ]
        
        # If response is long and contains data indicators, likely contains data
        return len(response) > 20 and any(data_indicators)
    
    def _extract_provided_data(self, response: str) -> Dict[str, Any]:
        """
        Extract structured data from user's natural language response.
        Uses pattern matching and parsing to extract specific data fields.
        
        Args:
            response: The user's response text
            
        Returns:
            Dict containing extracted data
        """
        try:
            extracted_data = {}
            response_lower = response.lower()
            
            # Extract dates
            dates = self._extract_dates(response)
            if dates:
                if len(dates) == 1:
                    extracted_data['start_date'] = dates[0]
                elif len(dates) == 2:
                    extracted_data['start_date'] = dates[0]
                    extracted_data['end_date'] = dates[1]
                else:
                    extracted_data['dates'] = dates
                    
            # Extract description
            description = self._extract_description(response)
            if description:
                extracted_data['description'] = description
                
            # Extract confidence score
            confidence = self._extract_confidence(response)
            if confidence is not None:
                extracted_data['confidence'] = confidence
                
            # Extract other structured data
            structured_data = self._extract_structured_data(response)
            extracted_data.update(structured_data)
            
            # If no structured data found, store raw response
            if not extracted_data:
                extracted_data = {
                    "raw_data": response,
                    "extracted_at": datetime.now(timezone.utc).isoformat()
                }
            else:
                extracted_data["raw_data"] = response
                extracted_data["extracted_at"] = datetime.now(timezone.utc).isoformat()
                
            return extracted_data
            
        except Exception as e:
            logger.error(f"❌ Error extracting data from response: {e}")
            return {
                "raw_data": response,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "extraction_error": str(e)
            }
    
    def _extract_dates(self, response: str) -> List[str]:
        """Extract dates from user response."""
        import re
        dates = []
        
        # ISO date pattern (YYYY-MM-DD)
        iso_dates = re.findall(r'\d{4}-\d{2}-\d{2}', response)
        dates.extend(iso_dates)
        
        # Month day, year pattern (January 15, 2023)
        month_day_year = re.findall(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}', response_lower)
        dates.extend(month_day_year)
        
        # Day month year pattern (15 January 2023)
        day_month_year = re.findall(r'\d{1,2}\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}', response_lower)
        dates.extend(day_month_year)
        
        return dates
    
    def _extract_description(self, response: str) -> Optional[str]:
        """Extract description from user response."""
        # Look for description patterns
        description_patterns = [
            r"description[:\s]+(.+)",
            r"it's\s+(.+)",
            r"it is\s+(.+)",
            r"the event was\s+(.+)",
            r"the wedding was\s+(.+)"
        ]
        
        for pattern in description_patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                return match.group(1).strip()
                
        # If response is long and doesn't contain dates, might be description
        if len(response) > 50 and not any(char.isdigit() for char in response):
            return response.strip()
            
        return None
    
    def _extract_confidence(self, response: str) -> Optional[float]:
        """Extract confidence score from user response."""
        import re
        
        # Look for numeric confidence scores
        confidence_patterns = [
            r"confidence[:\s]+(\d+\.?\d*)",
            r"(\d+\.?\d*)\s*%",
            r"(\d+\.?\d*)\s*out of 1",
            r"(\d+\.?\d*)\s*out of 10"
        ]
        
        for pattern in confidence_patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                try:
                    value = float(match.group(1))
                    # Convert percentage to decimal
                    if "%" in response or "out of 10" in response:
                        value = value / 100
                    elif "out of 1" in response:
                        pass  # Already decimal
                    return min(1.0, max(0.0, value))
                except ValueError:
                    continue
                    
        # Look for text-based confidence
        text_confidence = {
            "very high": 0.9, "high": 0.8, "medium": 0.5, "low": 0.3, "very low": 0.1,
            "certain": 0.95, "sure": 0.8, "confident": 0.7, "unsure": 0.4, "guessing": 0.2
        }
        
        for text, score in text_confidence.items():
            if text in response.lower():
                return score
                
        return None
    
    def _extract_structured_data(self, response: str) -> Dict[str, Any]:
        """Extract other structured data from user response."""
        structured_data = {}
        
        # Look for key-value pairs
        kv_patterns = [
            r"(\w+)[:\s]+([^,\n]+)",
            r"(\w+)\s*=\s*([^,\n]+)"
        ]
        
        for pattern in kv_patterns:
            matches = re.findall(pattern, response, re.IGNORECASE)
            for key, value in matches:
                key = key.lower().strip()
                value = value.strip()
                
                # Map common keys to standard fields
                if key in ["name", "label", "title"]:
                    structured_data["label"] = value
                elif key in ["type", "category"]:
                    structured_data["category"] = value
                elif key in ["location", "place", "where"]:
                    structured_data["location"] = value
                elif key in ["importance", "priority"]:
                    structured_data["importance"] = value
                else:
                    structured_data[key] = value
                    
        return structured_data
    
    def _extract_scheduling_info(self, response: str) -> tuple[Optional[datetime], Optional[int]]:
        """
        Extract scheduling information from user's response.
        
        Args:
            response: The user's response text
            
        Returns:
            Tuple of (ask_again_at datetime, ask_again_in_minutes)
        """
        response_lower = response.lower()
        
        # Look for time patterns
        if "tomorrow" in response_lower:
            # Schedule for tomorrow (simplified)
            from datetime import timedelta
            ask_again_at = datetime.now(timezone.utc) + timedelta(days=1)
            return ask_again_at, None
            
        # Look for "ask in X minutes" pattern
        import re
        minutes_match = re.search(r'ask in (\d+) minutes?', response_lower)
        if minutes_match:
            minutes = int(minutes_match.group(1))
            return None, minutes
            
        # Look for "ask in X hours" pattern
        hours_match = re.search(r'ask in (\d+) hours?', response_lower)
        if hours_match:
            hours = int(hours_match.group(1))
            return None, hours * 60
            
        # Default to 1 hour
        return None, 60
    
    def batch_ask_users(self, nodes: list[ProblematicNode]) -> list[UserResponse]:
        """
        Ask users about multiple nodes in sequence.
        
        Args:
            nodes: List of problematic nodes to ask about
            
        Returns:
            List of UserResponse objects
        """
        responses = []
        
        for node in nodes:
            try:
                response = self.ask_user_about_node(node)
                if response:
                    responses.append(response)
                else:
                    # Create a default "no idea" response
                    default_response = UserResponse(
                        node_id=node.id,
                        response_type=ResponseType.NO_IDEA,
                        raw_response="No response received",
                        responded_at=datetime.now(timezone.utc)
                    )
                    responses.append(default_response)
                    
            except Exception as e:
                logger.error(f"❌ Failed to ask user about node {node.id}: {e}")
                # Create error response
                error_response = UserResponse(
                    node_id=node.id,
                    response_type=ResponseType.NO_IDEA,
                    raw_response=f"Error: {e}",
                    responded_at=datetime.now(timezone.utc)
                )
                responses.append(error_response)
                
        return responses
    
    def ask_user_with_instructions(self, node: ProblematicNode) -> Optional[Dict[str, Any]]:
        """
        Ask user about a problematic node by delegating to questioner_manager
        and prepare actionable implementation instructions.
        
        Args:
            node: The problematic node to ask about
            
        Returns:
            Dict containing user response and implementation instructions, or None if failed
        """
        try:
            logger.info(f"❓ Asking user with instructions about node {node.id}: {node.problem_description}")
            
            # Create questioner_manager
            questioner_manager = self.multi_agent_factory.create_manager("questioner_manager")
            
            # Simple task - let the agent handle the details
            task = "Ask the user about a problematic node in the knowledge graph"
            
            # Prepare data dict - manager will unpack this to blackboard for agent access
            data = {
                "node_label": node.label,
                "node_semantic_label": getattr(node, 'semantic_label', ''),
                "node_description": node.description or '',
                "node_type": node.type,
                "node_category": node.category,
                "aliases": node.node_aliases or [],
                "start_date": node.start_date,
                "end_date": node.end_date,
                "valid_during": node.valid_during,
                "problem_instructions": node.resolution_notes or ''
            }
            
            # Add edge sentences for context if available
            if hasattr(node, 'full_node_info') and node.full_node_info:
                connections = node.full_node_info.get('connections', [])
                if connections:
                    # Get up to 3 edge sentences for context
                    edge_sentences = []
                    for connection in connections[:3]:
                        edge_sentence = connection.get('sentence', '')
                        if edge_sentence:
                            connected_node = connection.get('connected_node', {})
                            direction = connection.get('direction', 'unknown')
                            edge_type = connection.get('edge_type', 'unknown')
                            connected_label = connected_node.get('label', 'unknown')
                            
                            # Format: "Connected to [Node] via [EdgeType] ([Direction]): [Sentence]"
                            formatted_sentence = f"Connected to {connected_label} via {edge_type} ({direction}): {edge_sentence}"
                            edge_sentences.append(formatted_sentence)
                    
                    if edge_sentences:
                        data["edge_context"] = "\n".join(edge_sentences)
                        data["edge_count"] = len(connections)
            
            # Debug logging
            logger.info(f"🔍 Questioner data being passed: {data}")
            
            message = Message(
                data_type="agent_activation",
                sender="KG_Repair_Pipeline",
                receiver="questioner_manager",
                content=task,
                task=task,
                data=data,
                scope_context=build_system_scope_context(
                    owner_id="kg_repair_pipeline",
                    actor_id="kg_questioner",
                    surface="pipeline",
                    scope_id=f"scope::kg_repair::questioner::{node.id}",
                ),
            )
            
            # Execute via manager
            result = DI.manager_invoker.invoke(questioner_manager, message)
            
            if not result:
                logger.error(f"❌ questioner_manager returned no result for node {node.id}")
                return None
                
            # Parse user response from manager result (extracts from result.data)
            user_response = self._parse_manager_response(result, node)
            
            logger.info(f"✅ User responded to node {node.id}")
            logger.info(f"   Pause pipeline: {user_response.get('pause_entire_pipeline', False)}")
            logger.info(f"   Skip node: {user_response.get('skip_this_node', False)}")
            logger.info(f"   Instructions: {user_response.get('instructions', 'None')[:100] if user_response.get('instructions') else 'None'}")
            
            # Update database status
            try:
                self.processing_manager.update_node_status(
                    node_id=str(node.id),
                    status='questioned',
                    user_response=json.dumps(user_response),
                    user_response_type=user_response.get('response_type', 'unknown'),
                    user_provided_data=user_response.get('user_data'),
                    user_instructions=user_response.get('instructions'),
                    last_action='User questioned about node',
                    next_action='Implement fixes if provided'
                )
                logger.info(f"📝 Updated database status for node {node.id}")
            except Exception as e:
                logger.error(f"❌ Failed to update database status for node {node.id}: {e}")
            
            return user_response
            
        except Exception as e:
            logger.error(f"❌ Failed to ask user with instructions about node {node.id}: {e}")
            return None
    
    def _create_detailed_question_for_node(self, node: ProblematicNode) -> str:
        """
        Create a detailed question about a problematic node with context.
        
        Args:
            node: The problematic node to ask about
            
        Returns:
            Formatted question string
        """
        # Create a simple, clear question for the user
        question = f"""I found a problematic node in the knowledge graph:

**Node:** {node.label}
**Type:** {node.type}
**Problem:** {node.problem_description}

What would you like me to do with this node?"""
        
        return question
    
    def _format_attributes(self, attributes: Dict[str, Any]) -> str:
        """Format node attributes for display in the question."""
        if not attributes:
            return "No attributes available"
            
        formatted = []
        for key, value in attributes.items():
            if isinstance(value, (dict, list)):
                formatted.append(f"- {key}: {str(value)[:100]}...")
            else:
                formatted.append(f"- {key}: {value}")
                
        return "\n".join(formatted)
    
    def _generate_implementation_instructions(self, node: ProblematicNode, user_response: UserResponse) -> List[Dict[str, Any]]:
        """
        Generate actionable implementation instructions based on user response.
        
        Args:
            node: The problematic node
            user_response: User's response
            
        Returns:
            List of implementation instruction dictionaries
        """
        instructions = []
        
        try:
            if user_response.response_type == ResponseType.PROVIDE_DATA:
                # Generate instructions for updating the node with user data
                instructions.extend(self._generate_update_instructions(node, user_response))
                
            elif user_response.response_type == ResponseType.SKIP:
                # Generate instruction to mark node as skipped
                instructions.append({
                    "action": "mark_skipped",
                    "node_id": node.id,
                    "reason": "User requested to skip this node"
                })
                
            elif user_response.response_type == ResponseType.ASK_LATER:
                # Generate instruction to schedule for later
                instructions.append({
                    "action": "schedule_later",
                    "node_id": node.id,
                    "schedule_time": user_response.ask_again_at,
                    "reason": "User requested to ask again later"
                })
                
            elif user_response.response_type == ResponseType.NO_IDEA:
                # Generate instruction to mark as unknown
                instructions.append({
                    "action": "mark_unknown",
                    "node_id": node.id,
                    "reason": "User has no idea about this node"
                })
                
            elif user_response.response_type == ResponseType.INVALID:
                # Generate instruction to mark as invalid
                instructions.append({
                    "action": "mark_invalid",
                    "node_id": node.id,
                    "reason": "User says this is not a problem"
                })
                
            # Handle special commands
            if "hold off on all" in user_response.raw_response.lower():
                instructions.append({
                    "action": "stop_batch_processing",
                    "reason": "User requested to hold off on all nodes"
                })
                
        except Exception as e:
            logger.error(f"❌ Error generating implementation instructions: {e}")
            instructions.append({
                "action": "error",
                "node_id": node.id,
                "error": str(e)
            })
            
        return instructions
    
    def _generate_update_instructions(self, node: ProblematicNode, user_response: UserResponse) -> List[Dict[str, Any]]:
        """
        Generate update instructions for when user provides data.
        
        Args:
            node: The problematic node
            user_response: User's response with data
            
        Returns:
            List of update instruction dictionaries
        """
        instructions = []
        
        try:
            # Parse user-provided data
            user_data = user_response.provided_data or {}
            
            # Generate update instruction
            update_instruction = {
                "action": "update_node",
                "node_id": node.id,
                "updates": user_data,
                "reason": "User provided missing data"
            }
            instructions.append(update_instruction)
            
            # If this was an orphaned node, generate connection instructions
            if "orphaned" in node.problem_description.lower():
                connection_instruction = {
                    "action": "create_connections",
                    "node_id": node.id,
                    "connections": user_data.get('connections', []),
                    "reason": "User provided connection information for orphaned node"
                }
                instructions.append(connection_instruction)
                
        except Exception as e:
            logger.error(f"❌ Error generating update instructions: {e}")
            instructions.append({
                "action": "error",
                "node_id": node.id,
                "error": f"Update instruction generation failed: {e}"
            })
            
        return instructions
