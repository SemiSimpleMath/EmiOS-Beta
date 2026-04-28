from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum

class ResponseType(str, Enum):
    """Types of user responses"""
    PROVIDE_DATA = "provide_data"  # User provided the missing data
    SKIP = "skip"                   # User wants to skip this node
    ASK_LATER = "ask_later"         # User wants to be asked later
    NO_IDEA = "no_idea"             # User has no idea about this node
    INVALID = "invalid"             # User says this is not a real problem

class UserResponse(BaseModel):
    """
    Represents a user's response to a question about a problematic node.
    """
    node_id: str
    response_type: ResponseType
    raw_response: str  # The original user response text
    
    # Data provided by user (if response_type is PROVIDE_DATA)
    provided_data: Optional[Dict[str, Any]] = None
    
    # Scheduling information (if response_type is ASK_LATER)
    ask_again_at: Optional[datetime] = None
    ask_again_in_minutes: Optional[int] = None
    
    # Additional context
    user_notes: Optional[str] = None
    confidence_level: Optional[str] = None  # high, medium, low
    
    # Timestamps
    responded_at: datetime
    processed_at: Optional[datetime] = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
