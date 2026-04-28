from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    relationship: str = Field(description="One sentence: how the people are connected.")
    shared_history: str = Field(description="Key shared history and context with dates and gaps noted.")
    pending_goals: str = Field(description="Goals, tasks, or open loops related to this situation.")
    missing_edges: str = Field(description="Relationships implied but not recorded, and why they matter.")
    questions_for_user: str = Field(description="3-5 concrete questions the user should address.")
    researchable_via_kg: str = Field(description="2-3 narrow questions answerable by further KG queries.")
