from pydantic import BaseModel, Field
from typing import Optional, List, Union, Dict, Any

class SearchFilters(BaseModel):
    """Filters to restrict search scope with proper validation and guidance."""
    node_ids: Optional[List[str]] = Field(None, description="List of specific node UUIDs to restrict search to")
    node_types: Optional[List[str]] = Field(None, description="List of node types to filter by (e.g., 'Entity', 'Goal', 'State')")
    exclude_nodes: Optional[List[str]] = Field(None, description="List of node UUIDs to exclude from results")
    start_date: Optional[str] = Field(None, description="ISO date string - only include nodes valid after this date (e.g., '2024-01-01T00:00:00Z')")
    end_date: Optional[str] = Field(None, description="ISO date string - only include nodes valid before this date (e.g., '2024-12-31T23:59:59Z')")
    max_hops: Optional[int] = Field(None, description="Integer - expand node_ids to their neighborhoods (requires node_ids, default=all connected)")
    relationship_types: Optional[List[str]] = Field(None, description="List of relationship types for connected nodes (e.g., 'works_for', 'has_email')")
    text: Optional[str] = Field(None, description="Text to filter nodes by (searches in node labels)")

class kg_find_edge_args(BaseModel):
    node_id: str = Field(..., description="The UUID of the node to find edges for.")
    dir: Optional[str] = Field("both", description="Direction: 'in', 'out', or 'both' (default: 'both')")
    k: Optional[int] = Field(5, description="Maximum number of edges to return (default: 5)")
    text: Optional[str] = Field(None, description="Optional text to filter edges by (searches in connected node labels)")
    filters: Optional[SearchFilters] = Field(None, description="Optional additional filters to restrict search scope")


class kg_find_edge_arguments(BaseModel):
    tool_name: str
    arguments: kg_find_edge_args

