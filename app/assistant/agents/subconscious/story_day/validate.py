"""The day story's rules (agent_runtime/correction.py). Moved from brain_stories.day_problems (2026-11-15).

Refused: an empty headline or overview.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    return [Problem(why=f"the {f} is empty", field=f) for f in ("headline", "overview")
            if not str(answer.get(f) or "").strip()]
