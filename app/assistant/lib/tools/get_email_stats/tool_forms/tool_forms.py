from typing import Optional
from pydantic import BaseModel


class get_email_stats_args(BaseModel):
    query: str
    max_results: Optional[int] = 200
    group_by_sender: Optional[bool] = True
    group_by_day: Optional[bool] = False
    top_senders_limit: Optional[int] = 10


class get_email_stats_arguments(BaseModel):
    tool_name: str
    arguments: get_email_stats_args
