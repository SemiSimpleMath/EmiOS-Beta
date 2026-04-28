from pydantic import BaseModel


class search_web_args(BaseModel):
    query: str
    task: str
class search_web_arguments(BaseModel):
    tool_name: str
    arguments: search_web_args