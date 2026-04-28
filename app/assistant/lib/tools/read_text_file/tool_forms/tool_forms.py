from typing import Optional

from pydantic import BaseModel


class read_text_file_args(BaseModel):
    file_path: str
    start_marker: Optional[str] = None
    end_marker: Optional[str] = None
    include_markers: Optional[bool] = False
    max_chars: Optional[int] = None


class read_text_file_arguments(BaseModel):
    tool_name: str
    arguments: read_text_file_args
