from pydantic import BaseModel, Field


class web_modal_scan_args(BaseModel):
    pass


class web_modal_scan_arguments(BaseModel):
    tool_name: str
    arguments: web_modal_scan_args
