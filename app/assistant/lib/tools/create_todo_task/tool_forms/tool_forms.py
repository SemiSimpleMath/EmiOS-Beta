# Add Task Models
from typing import Optional

from pydantic import BaseModel


class create_todo_task_args(BaseModel):
    task_name: str
    due_date: Optional[str]
    priority: Optional[str]
    tasklist_name: Optional[str]
    parent_task_id: Optional[str]


class create_todo_task_arguments(BaseModel):
    tool_name: str
    arguments: create_todo_task_args
