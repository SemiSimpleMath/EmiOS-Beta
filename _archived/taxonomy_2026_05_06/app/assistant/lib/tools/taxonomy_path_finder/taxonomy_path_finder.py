"""
Taxonomy Path Finder Tool

Finds the most relevant taxonomy paths within a specific top-level category based on a description.
"""

import json

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message
from app.assistant.kg_core.taxonomy.utils import get_category_info
from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy
from app.assistant.agent_registry.agent_factory import AgentFactory



class TaxonomyPathFinderTool(BaseTool):
    """Tool for finding relevant taxonomy paths."""
    
    def __init__(self):
        super().__init__('taxonomy_path_finder')
    
    def _get_all_taxonomy_paths(self, session, top_level_taxonomy):
        """
        Get all taxonomy paths starting from the specified top-level category.
        
        Args:
            session: Database session
            top_level_taxonomy: The root category name (e.g., 'entity', 'state', 'event')
        
        Returns:
            List of full taxonomy paths (e.g., ["entity > artifact > machine > robot"])
        """
        # Find the root category
        root_category = session.query(Taxonomy).filter(
            Taxonomy.label.ilike(top_level_taxonomy),
            Taxonomy.parent_id.is_(None)
        ).first()
        
        if not root_category:
            return []
        
        def _get_paths_recursive(category, current_path=""):
            """Recursively get all paths from a category."""
            paths = []
            
            # Build current path
            if current_path:
                full_path = f"{current_path} > {category.label}"
            else:
                full_path = category.label
            
            # Add this path
            paths.append(full_path)
            
            # Get children and recurse
            children = session.query(Taxonomy).filter(Taxonomy.parent_id == category.id).all()
            for child in children:
                child_paths = _get_paths_recursive(child, full_path)
                paths.extend(child_paths)
            
            return paths
        
        return _get_paths_recursive(root_category)
    
    
    def _semantic_filter_paths(self, all_paths, keywords, threshold=0.3):
        """
        Filter paths using semantic matching against taxonomy categories.
        
        Args:
            all_paths: List of all taxonomy paths
            keywords: List of keywords to match against
            threshold: Minimum similarity score to keep a path
            
        Returns:
            List of filtered paths that score well against keywords
        """
        if not keywords:
            return all_paths  # No filtering if no keywords
        
        filtered_paths = []
        
        for path in all_paths:
            # Split path into individual categories
            categories = [cat.strip() for cat in path.split(' > ')]
            
            # Check if any category in the path scores well against any keyword
            path_scores = []
            for category in categories:
                category_lower = category.lower()
                for keyword in keywords:
                    keyword_lower = keyword.lower()
                    # Simple similarity scoring
                    if keyword_lower in category_lower:
                        path_scores.append(1.0)  # Exact match
                    elif any(word in category_lower for word in keyword_lower.split('_')):
                        path_scores.append(0.8)  # Partial match
                    elif any(word in category_lower for word in keyword_lower.split()):
                        path_scores.append(0.6)  # Word overlap
                    else:
                        # Check for semantic similarity (simple word overlap)
                        category_words = category_lower.split('_')
                        keyword_words = keyword_lower.split('_')
                        overlap = len(set(category_words) & set(keyword_words))
                        if overlap > 0:
                            path_scores.append(0.4)  # Some semantic overlap
            
            # Keep path if any category scored well against any keyword
            if path_scores and max(path_scores) >= threshold:
                filtered_paths.append(path)
        
        return filtered_paths
    
    def _call_taxonomy_lookup_agent(self, top_level_taxonomy, description, all_paths):
        """
        Call the taxonomy lookup agent to find relevant paths.
        
        Args:
            top_level_taxonomy: The root category name
            description: Description of what paths to find
            all_paths: List of all available paths
        
        Returns:
            List of relevant paths with scores and reasoning
        """
        try:

            agent = DI.agent_factory.create_agent("taxonomy_lookup::taxonomy_lookup_agent")
            
            # Prepare input
            agent_input = {
                "top_level_taxonomy": top_level_taxonomy,
                "description": description,
                "all_paths": "\n".join(all_paths),
                "total_paths": len(all_paths)
            }
            
            # Call agent
            message = Message(agent_input=agent_input)
            response = agent.action_handler(message)
            
            # The agent returns a ToolResult with data field containing 'relevant_paths'
            # Extract the list of paths from the data field
            if hasattr(response, 'data') and response.data:
                return response.data.get('relevant_paths', [])
            else:
                return []
                
        except Exception as e:
            print(f"Error calling taxonomy lookup agent: {e}")
            return []
    
    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Execute the taxonomy path finder tool.
        
        Args:
            tool_message: Tool message containing arguments
            
        Returns:
            ToolResult with relevant paths
        """
        try:
            # Extract arguments
            args = tool_message.tool_data.get('arguments', {})
            top_level_taxonomy = args.get('top_level_taxonomy')
            description = args.get('description')
            keywords = args.get('keywords', []) or []
            
            if not top_level_taxonomy or not description:
                return ToolResult(
                    result_type='error',
                    content="Missing required arguments: top_level_taxonomy and description"
                )
            
            # Get database session
            session = get_session()
            
            # Get all taxonomy paths from the specified top-level category
            all_paths = self._get_all_taxonomy_paths(session, top_level_taxonomy)
            
            if not all_paths:
                return ToolResult(
                    result_type='error',
                    content=f"No taxonomy paths found for top-level category: {top_level_taxonomy}"
                )
            
            # Apply semantic filtering to reduce the path space
            if keywords:
                filtered_paths = self._semantic_filter_paths(all_paths, keywords)
                
                # If filtering removed too many paths, use a lower threshold
                if len(filtered_paths) < 5 and len(all_paths) > 20:
                    filtered_paths = self._semantic_filter_paths(all_paths, keywords, threshold=0.2)
                
                # If still too few paths, use original paths
                if len(filtered_paths) < 3:
                    filtered_paths = all_paths
            else:
                # No keywords provided, use all paths
                filtered_paths = all_paths
            
            # Call the taxonomy lookup agent with filtered paths
            relevant_paths = self._call_taxonomy_lookup_agent(top_level_taxonomy, description, filtered_paths)
            
            # Return just the list of paths as content
            return ToolResult(
                result_type='taxonomy_paths',
                content=json.dumps(relevant_paths)
            )
            
        except Exception as e:
            return ToolResult(
                result_type='error',
                content=f"Error finding taxonomy paths: {str(e)}"
            )
        finally:
            if 'session' in locals():
                session.close()


def get_tool_class():
    """Return the tool class."""
    return TaxonomyPathFinderTool
