from pydantic import BaseModel


class run_task_args(BaseModel):
    compiled_file: str | None = None
    task_name: str | None = None
    task_query: str | None = None
    run_id: str | None = None
    event_name: str | None = None
    event_payload: dict | None = None
    initial_context: dict | None = None


class run_task_arguments(BaseModel):
    tool_name: str
    arguments: run_task_args

