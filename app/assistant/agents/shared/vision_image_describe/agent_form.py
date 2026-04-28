
from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """
    Structured output for `shared::vision_image_describe`.
    """
    description: str

