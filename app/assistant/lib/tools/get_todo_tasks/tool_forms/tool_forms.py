# Get Tasks Models
from typing import Optional

from pydantic import BaseModel


class get_todo_tasks_args(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    include_completed: Optional[bool] = False

class get_todo_tasks_arguments(BaseModel):
    tool_name: str
    arguments: get_todo_tasks_args

