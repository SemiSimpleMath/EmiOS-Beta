from typing import List, Optional
from pydantic import BaseModel


class TimelineEntry(BaseModel):
    # IMPORTANT: LLM outputs LOCAL TIME ONLY (no timezone, no Z, no offset).
    # Python code converts local->UTC deterministically before saving.
    start_local: str  # "YYYY-MM-DDTHH:MM:SS" local time (naive)
    end_local: str    # "YYYY-MM-DDTHH:MM:SS" local time (naive)
    label: str  # Location name (e.g. "Home", "Traveling to Doctor")
    city: str = ""      # e.g. "Irvine"
    state: str = ""     # e.g. "CA"
    country: str = ""   # e.g. "US"
    confidence: float = 0.7
    reasoning: str = ""


class AgentForm(BaseModel):
    timeline_entries: List[TimelineEntry]
    reasoning: str = ""  # Overall reasoning
