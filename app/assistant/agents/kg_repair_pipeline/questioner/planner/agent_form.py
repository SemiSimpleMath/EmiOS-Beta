from pydantic import BaseModel
from typing import List, Dict, Any, Union
from datetime import datetime, timezone

class FinalAnswer(BaseModel):
    """Structured response from the questioner after user interaction"""
    pause_entire_pipeline: bool = None
    skip_this_node: bool = None
    postpone_until: str = None  # ISO datetime string (e.g., "2025-09-30T15:00:00") or natural language
    instructions: str = None  # User's actionable instructions for fixing the node

class AgentForm(BaseModel):
    what_i_am_thinking: str
    action: str
    action_input: str
    final_answer: FinalAnswer = None  # Populated when exiting with return_control
