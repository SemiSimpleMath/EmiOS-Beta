"""The card section tagger's rule (agent_runtime/correction.py). Moved from entity_cards_v2.builder._tagger_problems
(2026-11-15). The card builder gives `fact_ids` (the candidate facts' ids) in agent_input.

Refused: a fact with no tagged_facts entry. An untagged fact would silently leave the card.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    tagged = {tf.get("fact_id") for tf in (answer.get("tagged_facts") or []) if isinstance(tf, dict)}
    return [Problem(why="it has no tagged_facts entry: tag every fact (or 'reject' it)", field="tagged_facts",
                    value=fid, structural=True)
            for fid in agent_input["fact_ids"] if fid not in tagged]
