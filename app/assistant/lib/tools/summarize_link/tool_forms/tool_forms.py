from pydantic import BaseModel


class summarize_link_args(BaseModel):
    url: str
    focus: str | None = None


class summarize_link_arguments(BaseModel):
    tool_name: str
    arguments: summarize_link_args
