from typing import Optional

from pydantic import BaseModel


class get_email_args(BaseModel):
    start_date: str
    end_date: str
    unseen: Optional[bool] = True  # Default to fetching only unseen/new emails
    repo_update: Optional[bool] = False  # Whether to update EventRepository
class get_email_arguments(BaseModel):
    tool_name: str
    arguments: get_email_args