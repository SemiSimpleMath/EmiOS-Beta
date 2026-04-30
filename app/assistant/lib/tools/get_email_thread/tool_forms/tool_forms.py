from typing import Optional

from pydantic import BaseModel


class get_email_thread_args(BaseModel):
    thread_id: Optional[str] = None
    seed_message_id: Optional[str] = None
    participant_email: Optional[str] = None
    max_messages: Optional[int] = 100
    include_body: Optional[bool] = True
    include_recent_messages: Optional[bool] = True
    recent_message_limit: Optional[int] = 8
    lookback_hours: Optional[int] = None


class get_email_thread_arguments(BaseModel):
    tool_name: str
    arguments: get_email_thread_args
