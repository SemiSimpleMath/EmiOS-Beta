from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    """Form for node cleanup analysis results"""
    
    suspect: bool = Field(
        description="Whether this node should be flagged as suspect for potential deletion"
    )
    
    suspect_reason: str = Field(
        description="Clear explanation of why this node is suspect",
    )
    
    confidence: float = Field(
        description="Confidence level in the assessment (0.0-1.0)",
        ge=0.0,
        le=1.0
    )
    
    cleanup_priority: str = Field(
        description="Priority level for cleanup: 'high', 'medium', 'low', or 'none'",
        default="none"
    )
    
    suggested_action: str = Field(
        description="Suggested action: 'delete', 'merge', 'convert_to_property', 'keep', or specific recommendation",
        default=None
    )
