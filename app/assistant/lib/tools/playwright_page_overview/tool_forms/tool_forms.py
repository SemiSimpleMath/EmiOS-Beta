from typing import Optional
from pydantic import BaseModel


class playwright_page_overview_args(BaseModel):
    wait_seconds: Optional[float] = 5.0
    wait_for_dom_ready: Optional[bool] = True
    scroll_pause_seconds: Optional[float] = 0.8
    resize_viewport: Optional[bool] = False
    top_question: Optional[str] = None
    bottom_question: Optional[str] = None


class playwright_page_overview_arguments(BaseModel):
    tool_name: str
    arguments: playwright_page_overview_args
