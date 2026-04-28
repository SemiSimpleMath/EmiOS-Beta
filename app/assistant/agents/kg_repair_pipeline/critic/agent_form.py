from pydantic import BaseModel, Field
from typing import List, Union


class AgentForm(BaseModel):
    analyzer_is_correct: bool
    reason: str
    is_problematic: bool
    suggestions: str
    additional_issues: str
