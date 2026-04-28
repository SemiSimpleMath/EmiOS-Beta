from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(
        default="",
        description="Conversational reply guiding the user through the document content.",
    )
    update_doc_tf: bool = Field(
        default=False,
        description=(
            "Set True when the conversation has produced content that should update the document. "
            "For example: user provided text to add, asked to remove a section, or gave specific edits. "
            "Set False when just chatting, asking questions, or greeting."
        ),
    )
    edit_instruction: str = Field(
        default="",
        description=(
            "REQUIRED when update_doc_tf is True. A crisp, unambiguous instruction for the doc writer "
            "agent describing EXACTLY what to change. Resolve all pronouns, disambiguate names, and "
            "be specific about what sections/entries/fields are affected. "
            "Examples: "
            "'Remove the entry for djnak@mymailstation.com' | "
            "'Change chess.com status from REVIEW to OK' | "
            "'Remove all `Run:` and `Run timestamp:` blocks, keeping only the sender entries.'"
        ),
    )
    doc_creation_done_tf: bool = Field(
        default=False,
        description=(
            "Set True only when the user explicitly confirms the document is complete and "
            "ready to save/export. Never set this speculatively."
        ),
    )
