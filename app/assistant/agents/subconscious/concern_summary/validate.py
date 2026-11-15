"""The concern summary's rule (agent_runtime/correction.py). Moved from concern_summary.write_summary (2026-11-15).

Refused: an answer with no summary.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    return [] if str(answer.get("summary") or "").strip() else [Problem(why="the answer had no summary",
                                                                        field="summary")]
