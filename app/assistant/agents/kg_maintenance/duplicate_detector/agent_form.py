from pydantic import BaseModel, Field
from typing import List

class MergeAction(BaseModel):
    """A merge action - simple and focused"""
    
    merge: List[str] = Field(
        description="List of node IDs to merge together (order doesn't matter - we'll automatically merge into the node with most connections)",
        min_items=2
    )
    
    labels: List[str] = Field(
        description="Corresponding labels for the nodes being merged",
        min_items=2
    )
    
    reason: str = Field(
        description="Brief reason why these nodes should be merged (max 100 chars)",
    )

class AgentForm(BaseModel):
    """Form for duplicate detector analysis - simple merge actions only"""
    reason: str
    merge_actions: List[MergeAction] = Field(
        description="List of merge actions to perform",
        default_factory=list
    )
    
    # Optional: overall assessment
    total_merges: int = Field(
        description="Total number of merge actions",
        default=0
    )
