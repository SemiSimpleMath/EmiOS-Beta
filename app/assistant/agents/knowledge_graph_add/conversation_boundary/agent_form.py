from pydantic import BaseModel, Field
from typing import List, Dict, Any

class Message(BaseModel):
    """A message in the conversation window"""
    id: str
    role: str
    message: str
    timestamp: str

class ConversationBounds(BaseModel):
    """Bounds of a self-contained conversation block"""
    start_message_id: str
    end_message_id: str
    should_process: bool
    reasoning: str

class MessageBounds(BaseModel):
    """Bounds for a specific message"""
    message_id: str
    bounds: ConversationBounds

class ConversationBoundaryInput(BaseModel):
    """Input for conversation boundary detection"""
    messages: List[Message]
    analysis_window_size: int

class AgentForm(BaseModel):
    """Output from conversation boundary detection"""
    message_bounds: List[MessageBounds]
    analysis_summary: str
