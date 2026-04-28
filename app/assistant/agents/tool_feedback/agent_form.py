from pydantic import BaseModel


class AgentForm(BaseModel):
    chat_str: str
    tts_str: str

