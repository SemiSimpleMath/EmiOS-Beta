from typing import List, Optional

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Output shape for the me::query_filter agent.

    The agent classifies the user's chat-input into ONE intent and fills the
    relevant fields. Frontend applies the result to its state.
    """

    intent: str = Field(
        default="noop",
        description=(
            "One of: set_seeds, add_seeds, set_time_range, set_time_mode, reset, noop. "
            "set_seeds replaces the current seed list. "
            "add_seeds appends to it. "
            "set_time_range enables range mode with from/to dates. "
            "set_time_mode switches between current and lifetime. "
            "reset clears seeds back to default and time to current. "
            "noop means the input was unparseable or empty — explain in `message`."
        ),
    )
    seed_node_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Node ids to seed personalized PageRank with. Required when intent is "
            "set_seeds or add_seeds. Empty otherwise. ALWAYS use ids from the "
            "Available nodes catalog supplied in the user prompt — never invent ids."
        ),
    )
    time_mode: Optional[str] = Field(
        default=None,
        description=(
            "Required when intent is set_time_mode. One of: current, lifetime. "
            "Empty/None for other intents."
        ),
    )
    time_from: Optional[str] = Field(
        default=None,
        description=(
            "ISO date (YYYY-MM-DD). Used with set_time_range. "
            "Year-only inputs ('2022') become 2022-01-01."
        ),
    )
    time_to: Optional[str] = Field(
        default=None,
        description=(
            "ISO date (YYYY-MM-DD). Used with set_time_range. "
            "Year-only inputs ('2024') become 2024-12-31."
        ),
    )
    message: str = Field(
        default="",
        description=(
            "Short user-visible summary of what was understood. e.g. "
            "'Focusing on Annika's family' or 'Filtering between 2022 and 2024' or "
            "'Sorry, I couldn't find a person named X'. Always populated."
        ),
    )
