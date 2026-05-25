from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(
        default="",
        description="Conversational reply guiding the user through the document content.",
    )
    doc_creation_done_tf: bool = Field(
        default=False,
        description=(
            "Set True only when the user explicitly confirms the document is complete and "
            "ready to save/export. Never set this speculatively."
        ),
    )
