from pydantic import BaseModel


class AgentForm(BaseModel):
    feed_str: str
    tts_str: str

