from typing import List
from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Annotate each input section with inline entity parentheticals.

    Input is a list of section strings (from pod_classifier). Output is a
    list of the same length, same order, where each string has entity
    parentheticals inserted after pronouns and vague references. Original
    words are preserved exactly — only parentheses are added.
    """

    annotated_sections: List[str] = Field(
        default_factory=list,
        description=(
            "List of annotated section strings. MUST be the same length as "
            "the input `sections` list, in the same order. Each entry is "
            "the corresponding input section with parentheticals inserted "
            "after pronouns and vague references. Preserve original words "
            "exactly — only add parentheses."
        ),
    )
    reasoning: str = Field(
        default="",
        description=(
            "Brief note on resolutions made. Empty if straightforward."
        ),
    )
