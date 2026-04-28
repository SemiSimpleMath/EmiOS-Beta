from pydantic import BaseModel
from typing import List

class ImportantEmail(BaseModel):
    sender: str
    subject: str
    priority: str
    summary: str
    action_needed: str
    urgency: str
    notes: str = None

class EmailInsight(BaseModel):
    insight_type: str
    description: str
    impact: str
    recommendation: str

class ResultForm(BaseModel):
    email_analysis: str
    important_emails: List[ImportantEmail]
    email_insights: List[EmailInsight]
class AgentForm(BaseModel):
    result: ResultForm
    action: str
    action_input: str

















