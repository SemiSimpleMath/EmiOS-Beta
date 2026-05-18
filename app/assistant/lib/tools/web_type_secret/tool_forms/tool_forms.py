from typing import Optional

from pydantic import BaseModel


class web_type_secret_args(BaseModel):
    pod_id: str
    projection: str
    ref: Optional[str] = None
    element: Optional[str] = None
    submit: bool = False
    clear_first: bool = True


class web_type_secret_arguments(BaseModel):
    tool_name: str
    arguments: web_type_secret_args
