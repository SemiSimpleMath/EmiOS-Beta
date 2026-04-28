from pydantic import BaseModel


class peak_at_link_args(BaseModel):
    url: str
    max_lines: int | None = 8
    max_chars_per_line: int | None = 220


class peak_at_link_arguments(BaseModel):
    tool_name: str
    arguments: peak_at_link_args
