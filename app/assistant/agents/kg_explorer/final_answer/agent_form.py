from typing import List
from pydantic import BaseModel, Field


class ActionableRecommendation(BaseModel):
    """A single actionable recommendation for the knowledge graph."""
    node_id: str = Field(..., description="The node this recommendation applies to, or empty string if not applicable")
    node_label: str = Field(..., description="Label of the node, or empty string if not applicable")
    finding: str = Field(..., description="What was discovered during exploration")
    recommendation: str = Field(..., description="Specific action to take (e.g., 'Create edge', 'Update node', 'Infer date')")
    reasoning: str = Field(..., description="Why this recommendation makes sense")
    confidence: str = Field(..., description="Confidence level: high, medium, or low")


class AgentForm(BaseModel):
    what_i_am_thinking: str = Field(..., description="Analysis of the exploration results")
    exploration_summary: str = Field(..., description="Brief summary of what was explored and discovered")
    actionable_recommendations: List[ActionableRecommendation] = Field(..., description="List of concrete actions that can be taken")