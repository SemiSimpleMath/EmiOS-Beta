from pydantic import BaseModel
from typing import List


class TodoTask(BaseModel):
    task_name: str
    priority: str
    due_date: str = None
    estimated_time: str = None
    notes: str = None


class ResultSummary(BaseModel):
    todo_analysis: str
    prioritized_tasks: List[TodoTask]
    task_recommendations: str


class AgentForm(BaseModel):
    action: str
    action_input: str
    result: ResultSummary

















