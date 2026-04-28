from typing import List
from pydantic import BaseModel, Field

class EdgeCandidate(BaseModel):
    """An existing edge candidate for comparison"""
    id: str
    relationship_type: str
    source_node_label: str
    target_node_label: str
    sentence: str
    context_window: str
    sentence_window: str

class EdgeMergerInput(BaseModel):
    """Input for edge merger agent"""
    new_edge_data: str
    existing_edge_candidates: str

class AgentForm(BaseModel):
    """Output from edge merger agent"""
    reasoning: str
    merge_edges: bool
    merged_edge_id: str
    review_later: bool
    unified_type: str
