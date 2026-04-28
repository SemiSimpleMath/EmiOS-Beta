from typing import List
from pydantic import BaseModel, Field, create_model


class ResolvedMessage(BaseModel):
    """Resolution output for a single message."""
    resolved_text: str = Field(description="The message text with entity annotations inserted in parentheses. Preserve original words exactly, only add parentheticals.")
    reasoning: str = Field(default="", description="Brief note on resolutions made, or empty if straightforward.")


def build_dynamic_form(message_ids: List[str]) -> type:
    """Build a Pydantic model with one ResolvedMessage field per message ID.

    Example: given message_ids=["msg_abc", "msg_def"], creates:

        class DynamicResolverForm(BaseModel):
            msg_abc: ResolvedMessage
            msg_def: ResolvedMessage

    This guarantees 1:1 mapping between input messages and output resolutions.
    """
    fields = {}
    for msg_id in message_ids:
        safe_key = msg_id.replace("-", "_").replace(":", "_")
        fields[safe_key] = (ResolvedMessage, Field(description=f"Resolved text for message {msg_id}"))
    return create_model("DynamicResolverForm", **fields)


# Default fallback AgentForm (used when no dynamic schema is injected).
class AgentForm(BaseModel):
    resolved_sentences: List[ResolvedMessage]
