from pydantic import BaseModel


class web_page_coords_args(BaseModel):
    # Optional question to focus the vision agent.
    question: str = ""
    # Whether to capture full-page screenshot (slower). Default is viewport only.
    full_page: bool = False
    # Strict mode: if anything is unexpected, raise and crash the run (no silent fallback).
    strict: bool = True


class web_page_coords_arguments(BaseModel):
    tool_name: str
    arguments: web_page_coords_args

