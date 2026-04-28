from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime
from enum import Enum

from .problematic_node import ProblematicNode
from .user_response import UserResponse

class PipelineStage(str, Enum):
    """Pipeline execution stages"""
    INITIALIZED = "initialized"
    ANALYZING = "analyzing"
    CRITIQUING = "critiquing"
    QUESTIONING = "questioning"
    IMPLEMENTING = "implementing"
    COMPLETED = "completed"
    FAILED = "failed"

class PipelineState(BaseModel):
    """
    Tracks the state of the KG repair pipeline execution.
    """
    pipeline_id: str
    current_stage: PipelineStage = PipelineStage.INITIALIZED
    
    # Input/Output
    input_kg_info: Optional[Dict] = None
    output_kg_info: Optional[Dict] = None
    
    # Pipeline data
    problematic_nodes: List[ProblematicNode] = []
    user_responses: List[UserResponse] = []
    
    # Progress tracking
    total_nodes_identified: int = 0
    nodes_validated: int = 0
    nodes_questioned: int = 0
    nodes_resolved: int = 0
    nodes_skipped: int = 0
    
    # Configuration
    max_nodes_per_batch: int = 10  # Limit nodes processed in one run
    auto_skip_threshold: int = 3   # Skip nodes after N failed attempts
    
    # Timestamps
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    
    # Error handling
    errors: List[str] = []
    retry_count: int = 0
    max_retries: int = 3
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
    
    def get_nodes_by_status(self, status: str) -> List[ProblematicNode]:
        """Get all nodes with a specific status"""
        return [node for node in self.problematic_nodes if node.status == status]
    
    def get_pending_questions(self) -> List[ProblematicNode]:
        """Get nodes that need user questions"""
        return [node for node in self.problematic_nodes 
                if node.status in ["validated", "questioned"] and not node.user_response]
    
    def get_ready_for_implementation(self) -> List[ProblematicNode]:
        """Get nodes that have user responses and are ready for implementation"""
        return [node for node in self.problematic_nodes 
                if node.status == "questioned" and node.user_response and node.user_data]
    
    def update_progress(self):
        """Update progress counters based on current state"""
        self.total_nodes_identified = len(self.problematic_nodes)
        self.nodes_validated = len(self.get_nodes_by_status("validated"))
        self.nodes_questioned = len(self.get_nodes_by_status("questioned"))
        self.nodes_resolved = len(self.get_nodes_by_status("resolved"))
        self.nodes_skipped = len(self.get_nodes_by_status("skipped"))
