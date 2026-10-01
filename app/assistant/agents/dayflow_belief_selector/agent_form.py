from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    belief_keys: list[str] = Field(
        description="The belief_key of each belief in this catalog that changes what the assistant does today.")
    qualifier_keys: list[str] = Field(
        default_factory=list,
        description="The belief_key of each other belief of a selected belief's topic that states a condition "
                    "on it (its day or time, a limit, an exception, a reminder wish), for the writer to read "
                    "with it.")
    reasoning: str
