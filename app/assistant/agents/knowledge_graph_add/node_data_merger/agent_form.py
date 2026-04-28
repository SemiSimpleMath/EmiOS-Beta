from pydantic import BaseModel
from typing import List

class AgentForm(BaseModel):
    reasoning: str
    merged_aliases: List[str] = None
    merged_hash_tags: List[str] = None
    unified_semantic_label: str = None
    unified_goal_status: str = None
    unified_valid_during: str = None
    unified_category: str = None
    unified_start_date: str = None
    unified_end_date: str = None
    unified_start_date_confidence: str = None
    unified_end_date_confidence: str = None
    merge_confidence: float  # 0.0 to 1.0
