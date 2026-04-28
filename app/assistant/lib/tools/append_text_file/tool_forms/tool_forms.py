from typing import Optional

from pydantic import BaseModel


class append_text_file_args(BaseModel):
    file_path: str
    content: str
    ensure_newline: Optional[bool] = True


class append_text_file_arguments(BaseModel):
    tool_name: str
    arguments: append_text_file_args
