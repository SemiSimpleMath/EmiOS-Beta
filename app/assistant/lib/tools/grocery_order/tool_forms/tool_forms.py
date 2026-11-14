from typing import List, Literal, Optional

from pydantic import BaseModel


class grocery_order_args(BaseModel):
    action: Literal["get", "ordered", "delivered"]
    week_start: Optional[str] = None
    note: Optional[str] = None
    items: Optional[List[str]] = None


class grocery_order_arguments(BaseModel):
    tool_name: str
    arguments: grocery_order_args
