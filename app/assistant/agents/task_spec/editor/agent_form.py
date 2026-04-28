from typing import List

from pydantic import BaseModel, Field


class SearchReplace(BaseModel):
    old_text: str = Field(
        description="Exact text to find in the document. Must match verbatim.",
    )
    new_text: str = Field(
        description="Replacement text. Empty string to delete.",
    )


class AgentForm(BaseModel):
    no_changes: bool = Field(
        default=True,
        description="Set to true if the conversation does not contain spec changes. When true, edits are ignored.",
    )
    edits: List[SearchReplace] = Field(
        default_factory=list,
        description="List of search/replace edits. Ignored when no_changes is true.",
    )
