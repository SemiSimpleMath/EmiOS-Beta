from typing import Optional

from pydantic import BaseModel


class vision_image_scout_args(BaseModel):
    image_path: str
    question: Optional[str] = None


class vision_image_scout_arguments(BaseModel):
    tool_name: str
    arguments: vision_image_scout_args
