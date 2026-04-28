from typing import Optional

from pydantic import BaseModel


class write_text_file_args(BaseModel):
    file_path: str
    content: str
    ensure_newline: Optional[bool] = True


class write_text_file_arguments(BaseModel):
    tool_name: str
    arguments: write_text_file_args
