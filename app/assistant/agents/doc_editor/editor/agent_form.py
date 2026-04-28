from typing import List, Literal

from pydantic import BaseModel, Field

from app.assistant.utils.search_replace_editor import SearchReplaceBlock


class AgentForm(BaseModel):
    doc_action: Literal["update", "no_op"] = Field(
        default="no_op",
        description=(
            "Choose 'update' ONLY when the document content has genuinely changed this turn "
            "because the user provided new content, corrections, or explicit removals. "
            "Choose 'no_op' when the user is just chatting, confirming, asking questions, "
            "or saying 'done'/'save'/'looks good'. When in doubt, choose 'no_op'. "
            "When doc_action is 'no_op', edits MUST be empty."
        ),
    )
    edits: List[SearchReplaceBlock] = Field(
        default_factory=list,
        description=(
            "Ordered list of search-and-replace operations to apply to the document. "
            "Each edit finds old_text exactly in the current document and replaces it with new_text. "
            "MUST be empty when doc_action is 'no_op'."
        ),
    )
