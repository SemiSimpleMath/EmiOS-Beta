"""The evidence matcher's rules (agent_runtime/correction.py). Moved from matching.selection.select_records
(2026-11-15). The agent_input is the page matching.service.select_for_evidence makes: `catalog`, each record shown with
its `belief_key`.

Refused: a belief_key that is not in this catalog page. One identity per record, the belief_key the record carries
(2026-10-01: request-local S<n> labels were written back as S<belief number>); no fuzzy identity mapping and no
partial acceptance. A key given twice is not refused: the selection keeps it once. The form refuses an answer without
belief_keys or reasoning.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    allowed = [r["belief_key"] for r in agent_input["catalog"]]
    return [Problem(why="it is not a belief_key in this catalog; use only the belief_key values shown, copied exactly",
                    field="belief_keys", value=key, structural=True)
            for key in dict.fromkeys(answer["belief_keys"]) if key not in allowed]
