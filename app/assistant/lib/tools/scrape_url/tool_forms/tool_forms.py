from pydantic import BaseModel


class scrape_url_args(BaseModel):
    query: str = ""  # Make query optional with empty string default
    url: str

class scrape_url_arguments(BaseModel):
    tool_name: str
    arguments: scrape_url_args