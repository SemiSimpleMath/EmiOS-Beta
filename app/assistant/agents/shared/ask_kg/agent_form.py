from pydantic import BaseModel, Field


class Citation(BaseModel):
    ref_kind: str = Field(..., description="What is cited: 'edge' or 'node'.")
    ref_id: str = Field(..., description="UUID of the cited edge/node.")
    note: str = Field(..., description="Short reason this citation supports the answer.")


class AgentForm(BaseModel):
    answer: str = Field(..., description="Answer grounded only in provided evidence.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0..1 confidence based on evidence strength.")
    citations: list[Citation] = Field(default_factory=list, description="Citations tied to specific evidence IDs.")


try:
    Citation.model_rebuild()
    AgentForm.model_rebuild()
except Exception:
    pass
