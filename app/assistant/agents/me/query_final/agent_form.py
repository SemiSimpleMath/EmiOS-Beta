from typing import Optional

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Output for me::query_final.

    The manager runtime serializes only the standard final_answer_*
    envelope into the result payload, so the structured filter intent
    has to ride inside `final_answer_answer` as a JSON string. The
    parse-query endpoint JSON-decodes it back into typed fields.
    """

    final_answer_answer: str = Field(
        default="",
        description=(
            "JSON-encoded payload with EXACTLY these keys: "
            "intent (one of: set_seeds, add_seeds, set_time_range, set_time_mode, reset, noop), "
            "seed_node_ids (array of UUIDs from kg_query results — never invented), "
            "time_mode (one of: current, lifetime, or null), "
            "time_from (ISO date or null), "
            "time_to (ISO date or null), "
            "message (a short user-visible summary string). "
            'Example: {"intent":"set_seeds","seed_node_ids":["abc-123"],'
            '"time_mode":null,"time_from":null,"time_to":null,'
            '"message":"Focusing on Katy"}'
        ),
    )
    result_summary: str = Field(default="", description="One-sentence outcome.")
    final_answer_task: Optional[str] = ""
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""
