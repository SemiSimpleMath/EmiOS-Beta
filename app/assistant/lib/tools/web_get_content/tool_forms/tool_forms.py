from pydantic import BaseModel, Field


class web_get_content_args(BaseModel):
    max_chars: int = Field(default=15000, description="Maximum characters of text to return. Default 15000.")


class web_get_content_arguments(BaseModel):
    tool_name: str
    arguments: web_get_content_args
