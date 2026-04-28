from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    """Form for importance calculation results"""
    
    importance_score: float = Field(
        description="Importance score between 0.0 and 1.0",
        ge=0.0,
        le=1.0
    )
    
    reasoning: str = Field(
        description="Clear explanation of why this importance score was assigned",
    )
    
    confidence: float = Field(
        description="Confidence level in the importance assessment (0.0-1.0)",
        ge=0.0,
        le=1.0
    )
    
    edge_count_bonus: float = Field(
        description="Any bonus applied due to edge count rules (e.g., +0.5 for persons with >10 edges)",
        default=None
    )
    
    automatic_assignment: str = Field(
        description="If automatically assigned due to edge count, specify the rule (e.g., '>50 edges = 1.0')",
        default=None
    )
