from pydantic import BaseModel


class web_visual_scout_args(BaseModel):
    # Optional question to focus the prose scout.
    question: str = ""
    # Whether to capture full-page screenshot (slower). Default is viewport only.
    full_page: bool = False


class web_visual_scout_arguments(BaseModel):
    tool_name: str
    arguments: web_visual_scout_args

