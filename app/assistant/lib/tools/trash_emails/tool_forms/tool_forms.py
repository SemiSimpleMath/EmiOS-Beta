from typing import List, Optional

from pydantic import BaseModel


class trash_emails_args(BaseModel):
    senders: List[str]
    extra_query: Optional[str] = ""
    max_per_sender: Optional[int] = 1000
class trash_emails_arguments(BaseModel):
    tool_name: str
    arguments: trash_emails_args

