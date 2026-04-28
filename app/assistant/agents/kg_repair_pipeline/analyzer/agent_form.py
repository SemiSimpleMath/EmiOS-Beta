from pydantic import BaseModel, Field
from typing import List

class ProblematicNode(BaseModel):
    """Represents a problematic node with its issues"""
    node_id: str
    problem_description: str

class AgentForm(BaseModel):
    """Form for KG repair analysis results"""
    
    is_problematic: bool
    
    problematic_nodes: List[ProblematicNode]
    
    analysis_summary: str

    suggested_actions: str