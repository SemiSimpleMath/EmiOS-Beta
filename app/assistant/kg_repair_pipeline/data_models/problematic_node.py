from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class ProblematicNode(BaseModel):
    """
    Represents a node in the knowledge graph that has identified issues.
    """
    # Core node data
    id: str
    label: str
    type: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    
    # Temporal fields
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    start_date_confidence: Optional[str] = None
    end_date_confidence: Optional[str] = None
    valid_during: Optional[str] = None
    
    # Node metadata
    node_aliases: Optional[List[str]] = None
    
    # Full node context (for critic and implementer stages)
    full_node_info: Optional[dict] = None
    
    # Problem tracking
    problem_description: str

    # Pipeline tracking
    status: str = "identified"  # identified, validated, questioned, resolved, skipped
    user_response: Optional[str] = None
    user_data: Optional[dict] = None
    resolution_notes: Optional[str] = None
    
    # Timestamps
    identified_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
