from typing import List

from pydantic import BaseModel, Field


class Node(BaseModel):
    temp_id: str
    aliases: List[str]
    hash_tags: List[str]
    # Required strings. When the value is not applicable / not known, the
    # prompt instructs the LLM to emit an empty string "" rather than null.
    # This keeps the structured-output schema strict and avoids the
    # garbage-fragment failure mode that happens when a required `str` field
    # is instructed to "emit null".
    start_date: str
    start_date_confidence: str
    # Natural-language start anchor when exact date is unknown but a
    # relational anchor exists ("shortly after Sam was born", "during
    # Waldo's last year"). Empty string "" when not applicable.
    start_date_prose: str
    end_date: str
    end_date_confidence: str
    end_date_prose: str
    valid_during: str
    semantic_label: str
    goal_status: str
    confidence: float
    importance: float

class AgentForm(BaseModel):
    Nodes: List[Node]
