from typing import Optional

from pydantic import BaseModel


class send_email_args(BaseModel):
    to: str
    subject: Optional[str]
    body: Optional[str]
    account_id: Optional[str] = None


class send_email_arguments(BaseModel):
    tool_name: str
    arguments: send_email_args