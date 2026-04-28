"""
KG Describe Node Tool

A wrapper around the official describe_node function from kg_tools.py
that provides detailed node information for multiple nodes at once, including all properties and connections.
"""

import json
import uuid
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.kg_core.kg_utils.kg_tools import describe_node
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool


class KGDescribeNode(BaseTool):
    """Tool for describing multiple nodes with detailed information."""
    
    def __init__(self):
        super().__init__('kg_describe_node')
    
    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Execute the kg_describe_node tool for multiple nodes.
        
        Args:
            tool_message: ToolMessage containing node_ids list and optional parameters
            
        Returns:
            ToolResult with detailed information for all nodes
        """
        try:
            # Extract arguments from tool_message
            tool_data = tool_message.tool_data
            arguments = tool_data.get("arguments", {})
            
            node_ids = arguments.get("node_ids", [])
            max_edges = arguments.get("max_edges", 5)  # Default to 5 if not provided
            include_raw = arguments.get("include_raw", False)
            
            # Ensure max_edges is an integer
            if max_edges is None:
                max_edges = 5
            
            if not node_ids or not isinstance(node_ids, list):
                return ToolResult(
                    result_type="Error",
                    content="Missing required parameter: node_ids (must be a list)"
                )
            
            # Get database session
            try:
                session = DI.get_service('database_session')
            except AttributeError:
                # Fallback if DI service not available
                from app.models.base import get_session
                session = get_session()
            
            # Process all node IDs
            all_nodes_info = []
            errors = []
            
            for node_id_str in node_ids:
                try:
                    # Convert string node_id to UUID if needed
                    if isinstance(node_id_str, str):
                        try:
                            node_id = uuid.UUID(node_id_str)
                        except ValueError:
                            errors.append(f"Invalid node_id format: {node_id_str}")
                            continue
                    else:
                        node_id = node_id_str
                    
                    # Call the official describe_node function
                    print(f"🔍 [DEBUG] Calling describe_node with node_id={node_id}, max_edges={max_edges}")
                    node_info = describe_node(
                        node_id=node_id,
                        session=session,
                        max_edges=max_edges
                    )
                    print(f"🔍 [DEBUG] describe_node returned: {type(node_info)}, keys: {list(node_info.keys()) if isinstance(node_info, dict) else 'Not a dict'}")
                    
                    # Format edges to include only useful information: source/target node IDs, relationship type, and sentence
                    formatted_node_info = {
                        "id": node_info.get("id"),
                        "label": node_info.get("label"),
                        "semantic_label": node_info.get("semantic_label"),
                        "node_type": node_info.get("node_type"),
                        "category": node_info.get("category"),
                        "description": node_info.get("description"),
                        "aliases": node_info.get("aliases", []),
                        "attributes": node_info.get("attributes", {}),
                        "valid_during": node_info.get("valid_during"),
                        "hash_tags": node_info.get("hash_tags", []),
                        "goal_status": node_info.get("goal_status"),
                        "confidence": node_info.get("confidence"),
                        "importance": node_info.get("importance"),
                        "source": node_info.get("source"),
                        "start_date": node_info.get("start_date"),
                        "end_date": node_info.get("end_date"),
                        "start_date_confidence": node_info.get("start_date_confidence"),
                        "end_date_confidence": node_info.get("end_date_confidence"),
                        "created_at": node_info.get("created_at"),
                        "updated_at": node_info.get("updated_at"),
                        "outbound_edges": [],
                        "inbound_edges": []
                    }
                    
                    # Format outbound edges: this node -> target node
                    for edge in node_info.get("outbound_edges", []):
                        formatted_node_info["outbound_edges"].append({
                            "target_node_id": str(edge.get("to_node_id", "")),
                            "target_node_label": edge.get("to_node_label", "Unknown"),
                            "relationship_type": edge.get("edge_type", "Unknown"),
                            "sentence": edge.get("sentence", "")
                        })
                    
                    # Format inbound edges: source node -> this node
                    for edge in node_info.get("inbound_edges", []):
                        formatted_node_info["inbound_edges"].append({
                            "source_node_id": str(edge.get("from_node_id", "")),
                            "source_node_label": edge.get("from_node_label", "Unknown"),
                            "relationship_type": edge.get("edge_type", "Unknown"),
                            "sentence": edge.get("sentence", "")
                        })
                    
                    all_nodes_info.append(formatted_node_info)
                    
                except Exception as e:
                    errors.append(f"Failed to describe node {node_id_str}: {str(e)}")
                    continue
            
            # Prepare the response
            response_data = {
                "nodes": all_nodes_info,
                "total_nodes": len(all_nodes_info),
                "requested_nodes": len(node_ids),
                "errors": errors
            }
            
            # Format the response - always return JSON for agents
            content = json.dumps(response_data, indent=2, default=str)
            print(f"🔍 [DEBUG] Final content length: {len(content)}, nodes processed: {len(all_nodes_info)}")
            
            return ToolResult(
                result_type="node_descriptions",
                content=content,
                data=response_data
            )
            
        except Exception as e:
            return ToolResult(
                result_type="Error",
                content=f"Failed to describe nodes: {str(e)}"
            )


# Create the tool loader function
get_tool_class = create_tool_loader(KGDescribeNode)