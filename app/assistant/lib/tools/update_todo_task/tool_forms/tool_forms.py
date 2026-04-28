from typing import Optional, Union
from pydantic import BaseModel


class update_todo_task_args(BaseModel):
    task_id: str  # Required
    due_date: Union[str, None] = None
    priority: Union[str, None] = None
    days_offset: Union[int, None] = None
    tasklist_name: Union[str, None] = None
    completed: Union[bool, None] = None
    description: Union[str, None] = None  # ✅ Add this line


class update_todo_task_arguments(BaseModel):
    tool_name: str
    arguments: update_todo_task_args
