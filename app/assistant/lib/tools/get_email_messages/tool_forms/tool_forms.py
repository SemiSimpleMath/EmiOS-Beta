from typing import Optional

from pydantic import BaseModel


class get_email_messages_args(BaseModel):
    query: str
    max_results: Optional[int] = 25
    include_body: Optional[bool] = True
    include_headers: Optional[bool] = True
    body_max_chars: Optional[int] = 15000


class get_email_messages_arguments(BaseModel):
    tool_name: str
    arguments: get_email_messages_args

