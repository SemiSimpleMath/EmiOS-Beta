"""The identity sentence writer's rule (agent_runtime/correction.py). Moved from
identity_sentence.generate_identity_sentence (2026-11-15).

Refused: definable=true with no identity_sentence. A definable node with no sentence would be stamped as judged
undefinable and raised for review.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    if answer.get("definable") and not str(answer.get("identity_sentence") or "").strip():
        return [Problem(why="definable is true but there is no identity_sentence: write the sentence, or answer "
                            "definable false", field="identity_sentence", value=answer.get("identity_sentence"))]
    return []
