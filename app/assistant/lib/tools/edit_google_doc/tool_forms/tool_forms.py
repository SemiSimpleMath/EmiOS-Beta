from typing import Optional
from pydantic import BaseModel


class edit_google_doc_args(BaseModel):
    document_id: str
    operation: str
    find: Optional[str] = None
    replace_with: Optional[str] = None
    text: Optional[str] = None
    ensure_newline: Optional[bool] = True
    account_id: Optional[str] = None


class edit_google_doc_arguments(BaseModel):
    tool_name: str
    arguments: edit_google_doc_args
