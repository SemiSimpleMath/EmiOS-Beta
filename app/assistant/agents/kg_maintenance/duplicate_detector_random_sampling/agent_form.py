from pydantic import BaseModel, Field
from typing import List

class AgentForm(BaseModel):
    """Form for random sampling duplicate detection results"""
    
    duplicate_groups: List[List[int]] = Field(
        description="List of duplicate groups, each group contains enum IDs of potentially duplicate nodes",
        example=[[1, 45, 234], [12, 89], [5, 67, 123, 456]]
    )
    
    notes: str = Field(
        description="Brief notes about the duplicate detection process",
        default=""
    )
