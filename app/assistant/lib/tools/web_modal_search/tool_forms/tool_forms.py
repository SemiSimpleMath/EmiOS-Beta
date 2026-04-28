from pydantic import BaseModel, Field


class web_modal_search_args(BaseModel):
    query: str = Field(description="Text to search for inside the modal (e.g. 'caramel syrup').")


class web_modal_search_arguments(BaseModel):
    tool_name: str
    arguments: web_modal_search_args
