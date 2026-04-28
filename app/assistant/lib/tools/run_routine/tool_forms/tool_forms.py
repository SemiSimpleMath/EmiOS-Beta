from pydantic import BaseModel


class run_routine_args(BaseModel):
    routine_id: str | None = None
    routine_query: str | None = None
    routine_name: str | None = None
    target_date: str | None = None  # YYYY-MM-DD (optional override)


class run_routine_arguments(BaseModel):
    tool_name: str
    arguments: run_routine_args

