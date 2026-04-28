"""
Taxonomy Path Corrector Agent Form

Takes a critic's base path suggestion and generates the optimal full taxonomy path.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class PathCorrection(BaseModel):
    """A corrected taxonomy path suggestion."""
    
    corrected_path: str = Field(
        description="The complete corrected taxonomy path (e.g., 'entity > person > occupation > software_developer')"
    )
    
    reasoning: str = Field(
        description="Explanation of why this path is optimal and how it improves on the critic's suggestion"
    )
    
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this path correction (0.0-1.0)"
    )


class AgentForm(BaseModel):
    """Output schema for taxonomy path correction."""
    
    correction: PathCorrection = Field(
        description="The corrected taxonomy path"
    )
