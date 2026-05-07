from typing import Optional

from pydantic import BaseModel


class refresh_wiki_page_args(BaseModel):
    entity_label: str
    run_critic: Optional[bool] = True
    reason: Optional[str] = None


class refresh_wiki_page_arguments(BaseModel):
    tool_name: str
    arguments: refresh_wiki_page_args
