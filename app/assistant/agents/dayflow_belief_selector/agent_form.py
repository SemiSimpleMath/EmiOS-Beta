from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    belief_ids: list[str] = Field(description="The beliefs that change what the assistant does today.")
    qualifier_ids: list[str] = Field(
        default_factory=list,
        description="Other beliefs of a selected belief's topic that state a condition on it (its day or "
                    "time, a limit, an exception, a reminder wish), for the writer to read with it.")
    reasoning: str
