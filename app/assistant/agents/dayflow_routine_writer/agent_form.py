from pydantic import BaseModel, Field


class Line(BaseModel):
    text: str = Field(description="One guidance entry, citing each belief it rests on as [belief:belief_key].")
    belief_keys: list[str] = Field(
        description="The belief_key of every belief this entry rests on, exactly as supplied; empty when none.")


class Slot(BaseModel):
    slot: str = Field(description="The slot's key exactly as the scaffold shows it in [slot ...].")
    lines: list[Line] = Field(description="The guidance for this hour; none when nothing applies.")


class AgentForm(BaseModel):
    slots: list[Slot] = Field(description="Every slot of the scaffold, in scaffold order, each exactly once.")
    flags: list[Line] = Field(description="Flags & watch items: cross-cutting and whole-day items.")
    change_summary: str = Field(
        description=(
            "1-3 sentences describing what changed vs. the previous version, or "
            "'Initial generation' if this is the first run of the day. "
            "Examples: 'Dropped Acme Corp work blocks — user confirmed day off. Promoted afternoon walk.' "
            "This is shown in logs and can be injected as a diff signal for downstream agents."
        )
    )
