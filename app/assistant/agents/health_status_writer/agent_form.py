from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    markdown: str = Field(
        description=(
            "The full health status document in markdown. "
            "Covers chronic conditions, current sleep state, and active temporary issues. "
            "Compact, factual, scannable. No scheduling cadences, no food preferences, "
            "no routine items."
        )
    )
