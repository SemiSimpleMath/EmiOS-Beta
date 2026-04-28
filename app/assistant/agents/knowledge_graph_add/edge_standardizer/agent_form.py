from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    relationship_type: str
    relationship_descriptor: str
    reasoning: str
    dictionary_suggestion: str

