from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    final_answer_answer: str = Field(
        description="The node's synthesized outcome — what was produced / learned / decided. "
                    "Concise; this is what the caller of the node receives."
    )
    result_summary: str = Field(
        default="",
        description="One sentence (<=150 chars) recorded ONTO the node as its closing note.",
    )
    node_status: str = Field(
        description="Terminal verdict for the node, judged from the work actually done: "
                    "'complete' (its goal is met), 'abandoned' (it became moot / no longer "
                    "needed), or 'error' (it could not be completed)."
    )
