from typing import Optional, List

from pydantic import BaseModel


class get_news_args(BaseModel):
    feed_urls: List[str]


class get_news_arguments(BaseModel):
    tool_name: str
    arguments: get_news_args