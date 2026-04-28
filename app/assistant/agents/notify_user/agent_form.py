from pydantic import BaseModel


class AgentForm(BaseModel):
    tts_str: str
    formatted_str: str



