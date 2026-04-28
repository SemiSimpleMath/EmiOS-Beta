from pydantic import BaseModel
from typing import List

class ScheduleItem(BaseModel):
    time: str
    activity: str
    duration: str
    priority: str
    notes: str = None

class TimingRecommendation(BaseModel):
    activity: str
    recommended_time: str
    reason: str
    flexibility: str

class AgentForm(BaseModel):
    schedule_optimization: str
    timing_recommendations: List[TimingRecommendation]
    daily_schedule: List[ScheduleItem]
    action: str
    action_input: str

















