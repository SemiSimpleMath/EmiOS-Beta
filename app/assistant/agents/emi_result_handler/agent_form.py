from pydantic import BaseModel


class AgentForm(BaseModel):
    task: str
    answer: str
    interesting_info: str
    methods: str
    sources: str
    meta_data: str
    feed: str


