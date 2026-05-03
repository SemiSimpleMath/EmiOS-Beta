from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Output for wiki_inclusion_critic.

    Decides whether a single new fact (one sentence) is worth incorporating
    into a given wiki section's prose. Used at refresh time to gate which
    sections need regeneration after KG changes — most ephemeral chat-
    extracted facts get filtered out before the expensive prose writer runs.
    """
    reason: str = Field(
        description=(
            "One short sentence justifying the decision. State what the new "
            "fact says, what the section is about, and whether the fact "
            "meaningfully adds to (or contradicts) what's already there. "
            "Write the reason BEFORE settling on the include flag."
        ),
    )
    include: bool = Field(
        description=(
            "True if the fact is worth including in this section's prose. "
            "False if the fact is trivial / ephemeral / already implied by "
            "what's there / belongs in a different section. Lean toward "
            "False — most chat-extracted facts are noise."
        ),
    )
