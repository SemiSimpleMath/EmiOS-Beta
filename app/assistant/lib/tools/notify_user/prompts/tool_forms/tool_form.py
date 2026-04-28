from pydantic import BaseModel


class notify_user_args(BaseModel):
    notify_info: str

class ask_user_arguments(BaseModel):
    tool_name: str
    arguments: notify_user_args