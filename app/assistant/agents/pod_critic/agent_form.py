from typing import Literal
from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Critic verdict on a candidate pod.

    The critic is a rejection gate. It looks at Pass 1's output and decides
    whether the pod is worth minting. The rubric: a pod must contain
    knowledge, a preference, a memory, or an actionable signal about the
    user. Everything else is rejected.
    """

    accept: bool = Field(
        description=(
            "True if the candidate pod contains knowledge, preference, "
            "memory, or actionable content worth keeping. False to reject."
        ),
    )
    content_type: Literal[
        "knowledge", "preference", "memory", "actionable", "none"
    ] = Field(
        description=(
            "The dominant content type if accepted. 'none' if rejected.\n"
            "- knowledge: a non-trivial fact about the user or their world\n"
            "- preference: a like/dislike, choice criterion, or change in one\n"
            "- memory: a moment/exchange worth recalling that doesn't fit KG cleanly\n"
            "- actionable: a decision or trigger an agent should respond to"
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "One-sentence explanation of the verdict. On reject, name the "
            "failure mode: 'no substance', 'derivative — references prior "
            "signal', 'belongs in KG as entity fact', 'ack-only', "
            "'not about the user', etc."
        ),
    )
