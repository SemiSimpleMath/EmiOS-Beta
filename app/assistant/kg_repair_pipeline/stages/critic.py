"""
KG Critic Stage

Reviews and validates the analyzer's findings before proceeding to repair.
"""

from typing import Dict, Any, Optional
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
from app.assistant.kg_repair_pipeline.data_models.problematic_node import ProblematicNode

logger = get_logger(__name__)

class KGCritic:
    """
    Critiques the analyzer's findings and provides validation and suggestions.
    """
    
    def __init__(self):
        self.agent_name = "kg_repair_pipeline::critic"
        
    def critique_node_with_suggestions(self, problematic_node: ProblematicNode) -> Dict[str, Any]:
        """
        Critique a problematic node and provide repair suggestions.
        
        Args:
            problematic_node: The problematic node identified by the analyzer
            
        Returns:
            Dict containing critique results and suggestions
        """
        try:
            logger.info(f"🎯 Critiquing node: {problematic_node.label}")
            
            # Prepare agent input data (all values must be strings)
            import json
            
            # Extract full node info if available
            full_info = problematic_node.full_node_info or {}
            
            # Get neighborhood summary (top 5 connections) with edge sentences
            neighborhood_summary = ""
            if full_info.get('connections'):
                connections = full_info['connections'][:5]  # Limit to 5 for prompt
                neighbor_lines = []
                for edge in connections:
                    connected_node = edge.get('connected_node', {})
                    direction = edge.get('direction', 'unknown')
                    edge_type = edge.get('edge_type', 'unknown')
                    edge_sentence = edge.get('sentence', '')
                    
                    connected_label = connected_node.get('label', 'unknown')
                    connected_type = connected_node.get('type', 'unknown')
                    
                    # Format: "Node Label (Type) - Edge Type - Direction - Sentence"
                    line = f"  - {connected_label} ({connected_type}) - {edge_type} - {direction}"
                    if edge_sentence:
                        line += f' - "{edge_sentence}"'
                    neighbor_lines.append(line)
                neighborhood_summary = "\n".join(neighbor_lines)
            
            agent_input = {
                "node_label": problematic_node.label,
                "node_description": problematic_node.description or "",
                "node_type": problematic_node.type or "",
                "node_category": problematic_node.category or "",
                "node_aliases": json.dumps(problematic_node.node_aliases or []),
                "start_date": str(problematic_node.start_date) if problematic_node.start_date else "",
                "start_date_confidence": str(problematic_node.start_date_confidence) if problematic_node.start_date_confidence else "",
                "end_date": str(problematic_node.end_date) if problematic_node.end_date else "",
                "end_date_confidence": str(problematic_node.end_date_confidence) if problematic_node.end_date_confidence else "",
                "valid_during": problematic_node.valid_during or "",
                "edge_count": str(full_info.get('edge_count', 0)),
                "neighborhood_sample": neighborhood_summary or "No connections",
                "confidence": str(full_info.get('confidence', '')),
                "importance": str(full_info.get('importance', '')),
                "source": full_info.get('source', ''),
                "attributes": json.dumps(full_info.get('attributes', {})),
                "problem_description": problematic_node.problem_description,
                "analysis_summary": f"Analyzer found: {problematic_node.problem_description}"
            }
            
            message = Message(agent_input=agent_input)
            
            # Get the critic agent
            critic_agent = DI.agent_factory.create_agent(self.agent_name)
            
            # Run the critique
            result = critic_agent.action_handler(message)
            
            if not result:
                logger.warning("⚠️ Critic returned no results")
                return {
                    "is_valid": False,
                    "is_problematic": False,
                    "analyzer_is_correct": False,
                    "reason": "No critique result",
                    "suggestions": "Unable to provide suggestions",
                    "priority": "low",
                    "additional_issues": ""
                }
                
            # Extract results from the agent's output
            critique_data = result.data if hasattr(result, 'data') else {}
            
            # Parse the critique results using ACTUAL agent output fields
            analyzer_is_correct = critique_data.get('analyzer_is_correct', False)
            is_problematic = critique_data.get('is_problematic', False)
            reason = critique_data.get('reason', 'No reason provided')
            suggestions = critique_data.get('suggestions', 'No suggestions provided')
            priority = critique_data.get('priority', 'medium')
            additional_issues = critique_data.get('additional_issues', '')
            
            # Determine if this is a valid problem to process
            # Valid if: analyst was correct OR node is problematic regardless
            is_valid = analyzer_is_correct or is_problematic
            
            logger.info(f"✅ Critique complete - Analyzer Correct: {analyzer_is_correct}, Is Problematic: {is_problematic}, Valid: {is_valid}, Priority: {priority}")
            
            return {
                "is_valid": is_valid,  # Combined logic: valid if problematic OR analyzer was right
                "is_problematic": is_problematic,  # Pass through for orchestrator fallback
                "analyzer_is_correct": analyzer_is_correct,  # For debugging/logging
                "reason": reason,
                "suggestions": suggestions,
                "priority": priority,
                "additional_issues": additional_issues
            }
            
        except Exception as e:
            logger.error(f"❌ Critique failed: {e}")
            return {
                "is_valid": False,
                "is_problematic": False,
                "analyzer_is_correct": False,
                "reason": f"Critique error: {e}",
                "suggestions": "Unable to provide suggestions",
                "priority": "low",
                "additional_issues": ""
            }
    
    def critique_analysis_batch(self, analysis_results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Critique a batch of analysis results.
        
        Args:
            analysis_results: Results from the analyzer stage
            
        Returns:
            Dict containing batch critique results
        """
        try:
            logger.info("🎯 Critiquing analysis batch...")
            
            # For now, just return a summary critique
            # In the future, this could analyze the overall quality of the analysis
            
            return {
                "batch_valid": True,
                "summary": "Batch critique completed",
                "recommendations": "Proceed with individual node critiques"
            }
            
        except Exception as e:
            logger.error(f"❌ Batch critique failed: {e}")
            return {
                "batch_valid": False,
                "summary": f"Batch critique error: {e}",
                "recommendations": "Review analysis results manually"
            }