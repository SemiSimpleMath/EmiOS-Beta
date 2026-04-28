from typing import Literal

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Nano classifier output — which part of the spec is the user talking about?"""
    target: Literal[
        "title",
        "goal",
        "completion",
        "description",
        "add_step",
        "edit_step",
        "remove_step",
        "add_input",
        "no_spec_change",
    ] = Field(
        description=(
            "Which part of the task spec the user's message relates to. "
            "'no_spec_change' when the user is chatting, confirming, or asking a question."
        ),
    )
    step_index: int = Field(
        default=-1,
        description="For edit_step or remove_step: which step (0-based). -1 if not applicable.",
    )
    content: str = Field(
        default="",
        description="The relevant content extracted from the user's message. For title: the title text. For add_step: the step description. For edit_step: what changed.",
    )
