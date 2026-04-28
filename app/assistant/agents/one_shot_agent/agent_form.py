from pydantic import BaseModel, ConfigDict


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments_json: str
