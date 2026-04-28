from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    markdown: str = Field(
        description=(
            "The full updated dayflow routine document in markdown. "
            "Time-structured, day-specific, belief-enriched. "
            "Must cover the full remaining day from current time onward. "
            "Past sections that are fully resolved may be condensed into a single "
            "'## What has happened' summary block rather than dropped entirely, "
            "so the agent retains context about the day so far."
        )
    )
    change_summary: str = Field(
        description=(
            "1-3 sentences describing what changed vs. the previous version, or "
            "'Initial generation' if this is the first run of the day. "
            "Examples: 'Dropped Acme Corp work blocks — user confirmed day off. Promoted afternoon walk.' "
            "This is shown in logs and can be injected as a diff signal for downstream agents."
        )
    )
