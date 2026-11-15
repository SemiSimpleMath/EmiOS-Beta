"""The card section distiller's rules (agent_runtime/correction.py). Moved from
entity_cards_v2.builder._distiller_problems (2026-11-15). The card builder gives `bullet_ids` (the candidate bullets'
ids) and `max_bullets` (the section's cap) in agent_input.

The distiller exists to cut the section to its cap. Refused: no bullets, more bullets than the cap, a bullet with no
text, or a bullet whose source_bullet_ids are empty or name an id that is not a candidate bullet. Bullets have no key
of their own, so a refused answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    cap = int(agent_input["max_bullets"])
    bullets = answer.get("bullets") or []
    if not bullets:
        return [Problem(why=f"no bullets: return the most identity-defining bullets, at most {cap}",
                        field="bullets")]
    if len(bullets) > cap:
        return [Problem(why=f"{len(bullets)} bullets returned, more than max_bullets {cap}", field="bullets")]
    known = set(agent_input["bullet_ids"])
    out = []
    for n, b in enumerate(bullets):
        if not (b.get("bullet_text") or "").strip():
            out.append(Problem(why="the bullet has no bullet_text", field="bullets", at=f"[{n}].bullet_text"))
            continue
        ids = b.get("source_bullet_ids") or []
        if not ids or any(x not in known for x in ids):
            out.append(Problem(why="source_bullet_ids must name input bullet_ids only, at least one",
                               field="bullets", at=f"[{n}].source_bullet_ids", value=ids))
    return out
