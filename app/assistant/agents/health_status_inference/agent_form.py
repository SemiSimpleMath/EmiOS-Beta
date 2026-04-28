from pydantic import BaseModel, Field
from typing import List, Optional


class MentalState(BaseModel):
    """User's mental/emotional state."""
    mood: str = Field(description="neutral | positive | negative")
    stress_load: str = Field(description="neutral | elevated | high")
    anxiety: str = Field(description="low | neutral | elevated | high")
    mental_energy: str = Field(description="high | normal | low | depleted")
    social_capacity: str = Field(description="high | normal | low | very_low")


class CognitiveState(BaseModel):
    """User's cognitive state and capacity."""
    load: str = Field(description="Low | Medium | High")
    interruption_tolerance: str = Field(description="High | Medium | Low | Zero")
    focus_depth: str = Field(description="Scattered | Normal | Deep_Work")


class PhysicalState(BaseModel):
    """User's physical state."""
    energy_level: str = Field(description="Depleted | Low | Normal | High")
    pain_level: str = Field(description="none | mild | moderate | severe")


class Physiology(BaseModel):
    """User's physiological needs."""
    hunger_probability: str = Field(description="Low | Medium | High")
    hydration_need: str = Field(description="Low | Medium | High")
    caffeine_state: str = Field(description="Under-caffeinated | Optimal | Over-caffeinated | Cutoff-Reached")


class AgentForm(BaseModel):
    """Output schema for health_status_inference agent."""
    
    mental: MentalState = Field(
        description="User's mental and emotional state"
    )
    
    cognitive: CognitiveState = Field(
        description="User's cognitive load and focus capacity"
    )
    
    physical: PhysicalState = Field(
        description="User's physical energy and pain level"
    )
    
    physiology: Physiology = Field(
        description="User's physiological needs (hunger, hydration, caffeine)"
    )
    
    health_concerns_today: List[str] = Field(
        default_factory=list,
        description="Health issues mentioned by user today (e.g., 'Have the flu', 'Very anxious', 'Threw up'). List of strings."
    )
    
    general_health_assessment: str = Field(
        description="A meaningful 1-2 sentence summary of user's overall health state today. "
                    "Highlight main points (sleep quality, energy, pain, concerns). "
                    "Example: 'User slept poorly and is experiencing elevated anxiety. Energy is low but stable.'"
    )
