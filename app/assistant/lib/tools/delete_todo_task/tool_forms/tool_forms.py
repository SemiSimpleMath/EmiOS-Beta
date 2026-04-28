# Delete TodoTask
from typing import Optional
from pydantic import BaseModel, Field


class delete_todo_task_args(BaseModel):
    task_id: str = Field(..., description="The todo task ID to delete")
    cascade: Optional[bool] = Field(
        default=False,
        description="If True, also delete all linked children (reminders, sub-events) from the event hierarchy"
    )


class delete_todo_task_arguments(BaseModel):
    tool_name: str
    arguments: delete_todo_task_args
