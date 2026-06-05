from typing import Optional

from pydantic import BaseModel


class get_email_messages_args(BaseModel):
    query: str
    max_results: Optional[int] = 25


class get_email_messages_arguments(BaseModel):
    tool_name: str
    arguments: get_email_messages_args

