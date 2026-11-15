"""The work line's rule (agent_runtime/correction.py). Moved from work_line.write_line (2026-11-15).

Refused: an answer with no line.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    return [] if str(answer.get("line") or "").strip() else [Problem(why="the answer had no line", field="line")]
