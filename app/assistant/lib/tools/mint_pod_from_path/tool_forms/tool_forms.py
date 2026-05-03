"""Pydantic schemas for the mint_pod_from_path tool."""
from pydantic import BaseModel


class mint_pod_from_path_args(BaseModel):
    """Input schema for mint_pod_from_path."""
    path: str


class mint_pod_from_path_arguments(BaseModel):
    """Tool wrapper for mint_pod_from_path."""
    tool_name: str
    arguments: mint_pod_from_path_args
