from pydantic import BaseModel
from typing import List

class WebInfo(BaseModel):
    title: str
    content: str
    source: str
    relevance: str
    url: str = None

class AgentForm(BaseModel):
    web_analysis: str
    relevant_info: List[WebInfo]
    news_highlights: str
    action: str
    action_input: str

















