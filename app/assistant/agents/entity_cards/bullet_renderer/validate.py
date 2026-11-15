"""The card bullet renderer's rules (agent_runtime/correction.py). Moved from
entity_cards_v2.builder._sourced_bullet_problems (2026-11-15). The card builder gives `fact_ids` (the section's input
fact ids) in agent_input.

Refused: a bullet with no text, or a bullet whose source_fact_ids are empty or name an id that is not an input fact.
Dropping a fact is the renderer's call; a bullet is persisted with the sources it cites. Bullets have no key of their
own, so a refused answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    known = set(agent_input["fact_ids"])
    out = []
    for n, b in enumerate(answer.get("bullets") or []):
        if not (b.get("bullet_text") or "").strip():
            out.append(Problem(why="the bullet has no bullet_text", field="bullets", at=f"[{n}].bullet_text"))
            continue
        ids = b.get("source_fact_ids") or []
        if not ids or any(x not in known for x in ids):
            out.append(Problem(why="source_fact_ids must name input fact_ids only, at least one", field="bullets",
                               at=f"[{n}].source_fact_ids", value=ids))
    return out
