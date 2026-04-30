from typing import Optional
from pydantic import BaseModel


class create_google_doc_args(BaseModel):
    title: str
    initial_content: Optional[str] = None
class create_google_doc_arguments(BaseModel):
    tool_name: str
    arguments: create_google_doc_args
