from typing import Optional
from pydantic import BaseModel


class get_google_doc_args(BaseModel):
    document_id: Optional[str] = None
    name_search: Optional[str] = None
    include_body: Optional[bool] = True
    max_chars: Optional[int] = 20000
class get_google_doc_arguments(BaseModel):
    tool_name: str
    arguments: get_google_doc_args
