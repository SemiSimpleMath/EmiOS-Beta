from pydantic import BaseModel, Field
from typing import List, Dict, Any

class SelectedNode(BaseModel):
    """Represents a selected node with its selection reason"""
    node_id: str
    selection_reason: str

class AgentForm(BaseModel):
    """Form for KG explorer node selection results"""
    
    selected_nodes: List[SelectedNode] = Field(description="List of selected nodes with their IDs and selection reasons")
    selection_summary: str = Field(description="Summary of the node selection process")
    exploration_potential: str = Field(description="Assessment of the exploration potential of selected nodes")
