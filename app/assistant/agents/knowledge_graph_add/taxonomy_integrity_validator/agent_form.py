"""
Taxonomy Integrity Validator Agent Form

Analyzes taxonomy structure and returns a list of problems found.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class TaxonomyIssue(BaseModel):
    """A single problem found in the taxonomy."""
    
    category_ids: List[int] = Field(
        description="List of taxonomy IDs affected by this problem"
    )
    
    labels: List[str] = Field(
        description="List of category labels affected by this problem"
    )
    
    paths: List[str] = Field(
        description="Full paths of affected categories (e.g., 'entity > person > software_developer')"
    )
    
    problem: str = Field(
        description="Clear description of the problem"
    )
    
    actions: List[str] = Field(
        description="ORDERED list of specific actions to fix this problem. Each action must be one of: move_category(cat_id, parent_id), merge_categories(source_id, dest_id), rename_category(cat_id, new_label), update_description(cat_id, description)"
    )
    
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this assessment (0.0-1.0)"
    )


class AgentForm(BaseModel):
    """Output schema for taxonomy integrity validation."""
    
    issues: List[TaxonomyIssue] = Field(
        default=[],
        description="List of problems found in the taxonomy branch"
    )
