"""The card summary writer's rule (agent_runtime/correction.py). Moved from
entity_cards_v2.builder._build_summary_section (2026-11-15).

Refused: an empty summary. A card with no summary is a failed build; level_0 comes only from the summary.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    if not (answer.get("summary") or "").strip():
        return [Problem(why="the summary is empty: write the 2-3 sentence summary", field="summary",
                        value=answer.get("summary"))]
    return []
