"""The evidence digester's rule (agent_runtime/correction.py). Moved from evidence_digestion._generate_digest
(2026-11-15).

Refused: an empty digest. The digest replaces the folded rows in every prompt that reads the node's evidence, so an
empty one would hide them.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    if not str(answer.get("digest") or "").strip():
        return [Problem(why="the digest is empty: write the consolidated digest of the rows", field="digest",
                        value=answer.get("digest"))]
    return []
