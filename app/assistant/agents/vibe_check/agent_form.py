from pydantic import BaseModel, Field
from typing import List, Optional


class AudioTargets(BaseModel):
    """
    LLM-friendly audio feature sliders (0-100).

    These are mapped deterministically to dataset units (0..1 features, dB loudness, BPM tempo).
    """

    energy: int = Field(ge=0, le=100, description="Perceived energy/intensity (0-100)")
    valence: int = Field(ge=0, le=100, description="Mood positivity (0=sad/dark, 50=neutral, 100=happy/uplifting)")
    loudness: int = Field(ge=0, le=100, description="Perceived loudness/mastering level (0=quiet, 100=loud)")
    speechiness: int = Field(ge=0, le=100, description="Spoken-word/rap presence (0=none, 100=very speechy)")
    acousticness: int = Field(ge=0, le=100, description="Acoustic character (0=electrified, 100=very acoustic)")
    instrumentalness: int = Field(ge=0, le=100, description="Instrumental focus (0=vocals, 100=instrumental)")
    liveness: int = Field(ge=0, le=100, description="Live/room audience feel (0=studio, 100=very live)")
    tempo: int = Field(ge=0, le=100, description="Tempo (0=slow, 100=fast)")


class VibePhase(BaseModel):
    """A phase within the vibe plan, expressed as 0-100 audio sliders."""

    duration_minutes: int = Field(ge=5, le=60, description="How long this phase lasts (5-60 minutes)")

    # Hold phase
    targets: Optional[AudioTargets] = Field(
        default=None,
        description="Hold phase targets (0-100). Use when maintaining a steady vibe.",
    )

    # Gradient phase
    targets_start: Optional[AudioTargets] = Field(
        default=None,
        description="Gradient phase start targets (0-100).",
    )
    targets_end: Optional[AudioTargets] = Field(
        default=None,
        description="Gradient phase end targets (0-100).",
    )

    note: str = Field(description="Brief description of this phase's purpose (e.g., 'validate mood', 'gentle lift')")


class MusicFilters(BaseModel):
    """
    Optional song selection filters derived from user chat (e.g., "play blues for a while").

    These filters are used downstream to bias or constrain song selection. Keep them simple
    and stable across a plan unless the user changes their request.
    """

    include_genres: List[str] = Field(
        default_factory=list,
        description="Preferred genres to include (dataset genre names, e.g., 'blues', 'alt-rock'). Leave empty if no genre constraint.",
    )
    exclude_genres: List[str] = Field(
        default_factory=list,
        description="Genres to avoid (dataset genre names). Leave empty if no exclusions.",
    )
    include_artists: List[str] = Field(
        default_factory=list,
        description="Preferred artists to include (e.g., 'Metallica'). Leave empty if no artist constraint.",
    )
    exclude_artists: List[str] = Field(
        default_factory=list,
        description="Artists to avoid. Leave empty if no exclusions.",
    )
    include_keywords: List[str] = Field(
        default_factory=list,
        description="Optional keywords to bias selection (e.g., 'acoustic', 'live'). Leave empty if unused.",
    )
    include_related_artists: bool = Field(
        default=False,
        description=(
            "If true, treat include_artists as anchor artist(s) and expand them to include "
            "Apple Music related artists (cached). Use when user asks for something 'like' an artist."
        ),
    )
    note: Optional[str] = Field(
        default=None,
        description="Short explanation of why these filters apply (e.g., 'User asked for blues for a while').",
    )


class AgentForm(BaseModel):
    """
    Output schema for the vibe_check agent.
    
    Creates a time-bounded music vibe plan based on current context.
    The DJ Manager will interpolate 0-100 audio slider targets within phases.
    """
    
    # The narrative plan (most important - forces reasoning)
    verbal_plan: str = Field(
        description="Human-readable explanation of the music strategy. Include: current state assessment, what you're trying to achieve, and why. Example: 'User just had lunch and energy is dipping. Maintain steady medium energy to support afternoon focus. Keep vocals low for concentration.'"
    )
    
    # Context window awareness
    current_context_block: str = Field(
        description="What context block we're in (e.g., 'Work focus time', 'Free time', 'Family time', 'Wind-down')"
    )
    context_block_ends: str = Field(
        description="When this context block ends in local time (e.g., '4:30 PM', '11:00 PM')"
    )
    
    # Plan duration (max 60 min, can be shorter)
    plan_duration_minutes: int = Field(
        ge=15, le=60,
        description="How long this plan covers (15-60 minutes). Use shorter durations when context is uncertain."
    )
    
    # Phases (implements the verbal plan)
    phases: List[VibePhase] = Field(
        min_length=1, max_length=3,
        description="1-3 phases that implement the verbal plan. Total duration must equal plan_duration_minutes."
    )

    # Optional filters (e.g., user explicitly requested an artist/genre)
    music_filters: Optional[MusicFilters] = Field(
        default=None,
        description="Optional selection filters derived from recent user chat (artist/genre/etc). Use when user explicitly requests something like 'play X for a while'.",
    )

    # Extra high-level knob (optional)
    intensity: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Optional high-level intensity knob (0-100) to guide selection bias. "
            "Use when the user explicitly requests 'more intense', 'calmer', 'chill', etc. "
            "This should broadly correlate with energy + loudness + tempo."
        ),
    )
    
    # Current state assessment (from health data)
    current_mood: str = Field(
        description="User's current mood (e.g., 'focused', 'drained', 'frustrated', 'content')"
    )
    current_energy: str = Field(
        description="User's current energy level (e.g., 'depleted', 'low', 'moderate', 'high')"
    )
    anxiety_level: str = Field(
        description="User's current anxiety (e.g., 'calm', 'slight', 'moderate', 'elevated')"
    )
    
    # Continuity tracking (important for rechecks)
    is_continuation: bool = Field(
        description="True if this continues the previous plan direction, False if changing direction"
    )
    change_reason: Optional[str] = Field(
        default=None,
        description="If is_continuation=False, explain what changed (user input, health change, context block change). Required if changing direction."
    )
    
    # Reasoning
    reasoning: str = Field(
        description="Brief technical reasoning. On rechecks, state: 1) what previous plan was doing, 2) whether anything changed, 3) why continuing or changing."
    )
