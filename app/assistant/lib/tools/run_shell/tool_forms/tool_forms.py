"""Pydantic schemas for the run_shell tool."""
from pydantic import BaseModel


class run_shell_args(BaseModel):
    """Input schema for run_shell."""
    command: str
    reason: str


class run_shell_arguments(BaseModel):
    """Tool wrapper for run_shell."""
    tool_name: str
    arguments: run_shell_args
