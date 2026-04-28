from typing import List, Optional
from pydantic import BaseModel

class CalendarEvent(BaseModel):
    event_name: str
    date: str
    time: str
    importance: str
    notes: Optional[str] = None

class UpcomingEvent(BaseModel):
    event_name: str
    date: str
    days_until: str
    importance: str
    preparation_needed: str
    notes: Optional[str] = None

class ResultSummary(BaseModel):
    calendar_analysis: str
    summary_of_todays_calendar_events: str
    important_events: List[CalendarEvent]
    upcoming_important_events: List[UpcomingEvent]
    schedule_insights: str

class AgentForm(BaseModel):
    action: str
    action_input: str
    result: ResultSummary
