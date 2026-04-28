from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class save_daily_summary_args(BaseModel):
    summary_data: Dict[str, Any] = Field(..., description="The daily summary data dict.")
    date_str: Optional[str] = Field(default=None, description="Date in YYYY-MM-DD format. Defaults to today.")


class save_daily_summary_arguments(BaseModel):
    tool_name: str
    arguments: save_daily_summary_args
