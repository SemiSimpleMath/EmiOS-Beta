from typing import Optional

from pydantic import BaseModel


class get_important_emails_args(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    unseen: Optional[bool] = False
    account_id: Optional[str] = None


class get_important_emails_arguments(BaseModel):
    tool_name: str
    arguments: get_important_emails_args
