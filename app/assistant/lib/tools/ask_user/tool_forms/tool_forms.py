from pydantic import BaseModel


class ask_user_args(BaseModel):
    text: str

class ask_user_arguments(BaseModel):
    tool_name: str
    arguments: ask_user_args