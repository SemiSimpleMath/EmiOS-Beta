from typing import List, Optional

from pydantic import BaseModel, Field


class FinalAnswerDataItem(BaseModel):
    data_type: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    url: Optional[str] = None
    link: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None


class PodReference(BaseModel):
    pod_id: str = Field(description="Pod id holding the full finding / work behind the answer.")
    one_liner: str = Field(default="", description="Headline of what's in this pod.")


class AgentForm(BaseModel):
    final_answer_answer: str
    result_summary: str = Field(
        default="",
        description="One-sentence outcome for downstream agents (max ~150 chars).",
    )
    pod_references: List[PodReference] = Field(
        default_factory=list,
        description=(
            "When a research notebook is provided, list the pods that hold the full findings / "
            "work behind this answer (pod_id + one_liner). The reader opens these for complete "
            "detail; do not dump full pod bodies into final_answer_answer."
        ),
    )
    final_answer_sources: List[str] = Field(default_factory=list)
    final_answer_detail_level: str = "brief"
    final_answer_data_list: List[FinalAnswerDataItem] = Field(default_factory=list)
    final_answer_task: Optional[str] = ""
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""
