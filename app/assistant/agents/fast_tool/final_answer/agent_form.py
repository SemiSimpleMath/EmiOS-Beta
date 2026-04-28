from typing import List
from pydantic import BaseModel, ConfigDict, Field


class FinalAnswerDataItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_type: str | None = None
    key: str | None = None
    value: str | None = None
    url: str | None = None
    link: str | None = None
    title: str | None = None
    description: str | None = None
    source: str | None = None
    timestamp: str | None = None


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    final_answer_answer: str
    result_summary: str | None = Field(
        default="",
        description="One-sentence outcome for downstream agents (max ~150 chars). E.g. 'Set HVAC to 75F.' or 'Created calendar event for 3pm.'",
    )
    final_answer_sources: List[str]
    final_answer_detail_level: str
    final_answer_data_list: List[FinalAnswerDataItem]
    final_answer_task: str | None = ""
    final_answer_what_was_done: str | None = ""
    final_answer_interesting_info: str | None = ""
