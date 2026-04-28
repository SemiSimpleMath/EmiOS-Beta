from pydantic import BaseModel, Field
from typing import Optional, List, Literal


class SongCandidate(BaseModel):
    """A single song candidate with reasoning."""
    
    title: str = Field(description="Song title (no extra formatting).")

    artist: str = Field(description="Primary artist name (no extra formatting).")
    
    reasoning: str = Field(
        description="Brief explanation of why this song fits the current vibe targets"
    )

    source: Literal["provided"] = Field(
        description="Must be 'provided'. Candidates must be chosen from the provided list only (no invented songs)."
    )


class AgentForm(BaseModel):
    """
    Output from the DJ orchestrator agent.
    
    Provides 5-10 song candidates that match the current vibe targets.
    The DJ Manager will score these based on play history and randomly
    select one (less recently played songs have higher probability).
    """
    
    candidates: List[SongCandidate] = Field(
        description=(
            "Exactly 10 candidates total, all chosen from the provided list (source='provided'). "
            "Do not invent new songs."
        ),
        min_length=10,
        max_length=10,
    )
    
    vibe_interpretation: str = Field(
        description="Brief interpretation of the vibe targets (e.g., 'Low energy, slightly melancholic, instrumental preferred')"
    )
    
    skip_music: bool = Field(
        default=False,
        description="Set to true if music is not appropriate right now (e.g., meeting soon, user seems to want quiet)"
    )
    
    skip_reason: Optional[str] = Field(
        default=None,
        description="If skip_music is true, explain why"
    )
