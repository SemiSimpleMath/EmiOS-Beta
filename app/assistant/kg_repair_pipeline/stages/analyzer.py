"""
KG Analyzer Stage

Repurposes the existing KG Explorer analyzer to identify problematic nodes.
"""

from typing import Dict, List, Optional, Any
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class KGAnalyzer:
    """
    Analyzes the knowledge graph to identify problematic nodes.
    Repurposes the existing kg_explorer::analyzer::planner agent.
    """
    
    def __init__(self):
        self.agent_name = "kg_repair_pipeline::analyzer"
        
    def analyze_kg(self, kg_info: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Analyze the knowledge graph and identify problematic nodes.
        
        Args:
            kg_info: Optional knowledge graph information to analyze
            
        Returns:
            Dict containing analysis results and problematic nodes
        """
        try:
            logger.info("🔍 Starting KG analysis...")
            
            # Create analysis task
            task = "Analyze the knowledge graph to identify problematic nodes with missing data, structural issues, or quality problems"
            
            # Create message for the analyzer agent
            message = Message(
                data_type="agent_activation",
                sender="Pipeline",
                receiver="Analyzer",
                content="",
                task=task,
                information=kg_info
            )
            
            # Get the analyzer agent
            analyzer_agent = DI.agent_factory.create_agent(self.agent_name)
            
            # Run the analysis
            result = analyzer_agent.action_handler(message)
            
            if not result:
                logger.warning("⚠️ Analyzer returned no results")
                return {"problematic_nodes": []}
                
            # Extract results from the agent's output
            analysis_data = result.data if hasattr(result, 'data') and isinstance(result.data, dict) else {}

            # Parse the problematic nodes from the agent's structured output
            problematic_nodes = self._parse_analysis_results(analysis_data)
            
            logger.info(f"✅ Analysis complete - found {len(problematic_nodes)} problematic nodes")
            
            return {
                "analysis_summary": analysis_data.get('analysis_summary', 'Analysis completed'),
                "problematic_nodes": problematic_nodes
            }
            
        except Exception as e:
            logger.error(f"❌ KG analysis failed: {e}")
            return {"problematic_nodes": [], "error": str(e)}
    
    def _parse_analysis_results(self, analysis_data: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        Parse the analyzer's structured output to extract problematic nodes.
        
        Args:
            analysis_data: The structured output from the analyzer agent
            
        Returns:
            List of problematic node dictionaries
        """
        problematic_nodes = []
        
        try:
            # Get problematic nodes from the analysis data
            if 'problematic_nodes' in analysis_data:
                for node in analysis_data['problematic_nodes']:
                    if isinstance(node, dict) and 'node_id' in node and 'problem_description' in node:
                        problematic_nodes.append({
                            'node_id': node['node_id'],
                            'problem_description': node['problem_description']
                        })
                    elif isinstance(node, str):
                        # Handle case where nodes are stored as strings
                        # Try to parse "node_id: description" format
                        if ':' in node:
                            parts = node.split(':', 1)
                            if len(parts) == 2:
                                problematic_nodes.append({
                                    'node_id': parts[0].strip(),
                                    'problem_description': parts[1].strip()
                                })
            
            # If no structured data, try to extract from analysis summary
            if not problematic_nodes and 'analysis_summary' in analysis_data:
                # This would need more sophisticated parsing
                # For now, return empty list
                logger.warning("⚠️ No structured problematic nodes found in analysis results")
                
        except Exception as e:
            logger.error(f"❌ Error parsing analysis results: {e}")
            
        return problematic_nodes
    
    def analyze_single_node(self, node_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze a single node with its neighborhood data for problems.
        
        Args:
            node_info: Dictionary containing node information and neighborhood data
            
        Returns:
            Dict containing analysis results for the single node
        """
        try:
            logger.info(f"🔍 Analyzing single node: {node_info.get('label', 'Unknown')}")
            
            # Prepare agent input data (all values must be strings)
            import json
            
            # Get sample connections for the analyzer
            connections = node_info.get('connections', [])
            sample_connections = []
            
            # Format sample connections for the analyzer
            for i, connection in enumerate(connections[:3]):  # Limit to 3 sample connections
                connected_node = connection.get('connected_node', {})
                direction = connection.get('direction', 'unknown')
                edge_type = connection.get('edge_type', 'unknown')
                edge_sentence = connection.get('sentence', '')
                
                # Debug logging
                logger.info(f"🔍 Connection {i}: {connection}")
                logger.info(f"🔍 Connected node: {connected_node}")
                
                sample_connections.append({
                    "direction": direction,
                    "edge_type": edge_type,
                    "connected_node_label": connected_node.get('label', 'unknown'),
                    "connected_node_type": connected_node.get('type', 'unknown'),
                    "edge_sentence": edge_sentence
                })
            
            agent_input = {
                "node_label": node_info.get('label', ''),
                "node_semantic_label": node_info.get('semantic_label', ''),
                "node_description": node_info.get('description', ''),
                "node_type": node_info.get('node_type', ''),
                "node_category": node_info.get('category', ''),
                "node_aliases": json.dumps(node_info.get('aliases', [])),
                "start_date": str(node_info.get('start_date', '')) if node_info.get('start_date') else '',
                "start_date_confidence": str(node_info.get('start_date_confidence', '')) if node_info.get('start_date_confidence') else '',
                "end_date": str(node_info.get('end_date', '')) if node_info.get('end_date') else '',
                "end_date_confidence": str(node_info.get('end_date_confidence', '')) if node_info.get('end_date_confidence') else '',
                "valid_during": node_info.get('valid_during', ''),
                "edge_count": str(node_info.get('edge_count', 0)),
                "sample_connections": sample_connections  # Don't JSON serialize - pass as list
            }
            
            message = Message(agent_input=agent_input)
            
            analyzer_agent = DI.agent_factory.create_agent(self.agent_name)
            result = analyzer_agent.action_handler(message)
            
            if not result:
                return {"is_problematic": False, "problem_description": "No analysis result"}
                
            analysis_data = result.data if hasattr(result, 'data') and isinstance(result.data, dict) else {}

            # Parse single node analysis
            is_problematic = analysis_data.get('is_problematic', False)
            problem_description = analysis_data.get('analysis_summary', 'No issues found')
            
            return {
                "is_problematic": is_problematic,
                "problem_description": problem_description,
            }
            
        except Exception as e:
            logger.error(f"❌ Single node analysis failed: {e}")
            return {"is_problematic": False, "problem_description": f"Analysis error: {e}"}
    
    def _extract_priority(self, analysis_data: Dict[str, Any]) -> str:
        """
        Extract priority level from analysis data.
        
        Args:
            analysis_data: Analysis results from the agent
            
        Returns:
            Priority level (low, medium, high, critical)
        """
        summary = analysis_data.get('analysis_summary', '').lower()
        
        if any(word in summary for word in ['critical', 'urgent', 'severe']):
            return 'critical'
        elif any(word in summary for word in ['high', 'important', 'significant']):
            return 'high'
        elif any(word in summary for word in ['minor', 'low', 'trivial']):
            return 'low'
        else:
            return 'medium'
