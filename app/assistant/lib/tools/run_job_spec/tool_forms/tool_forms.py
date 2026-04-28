from pydantic import BaseModel


class run_job_spec_args(BaseModel):
    job_file: str
    orchestrator: str | None = None


class run_job_spec_arguments(BaseModel):
    tool_name: str
    arguments: run_job_spec_args
