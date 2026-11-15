"""Selection by each belief's own belief_key."""


def select_records(records, invoke, form):
    """invoke receives the records; the model answers with the belief_key of each record it selects
    (`form.belief_keys`).

    One identity per record, the belief_key the record itself carries (2026-10-01). Request-local
    S<n> labels beside the belief_key were written back as S<belief number>, failing the routine.
    Callers show the model each record with no other id fields beside the belief_key.

    The agent's validate.py refuses a key not in the records; a refused answer is corrected once in
    conversation and raises AgentOutputInvalid when still refused (agent_runtime/correction.py; until
    2026-11-15 this function re-asked with a `selection_retry`). Provider failures propagate.
    """
    if not records:
        return [], "Empty catalog"
    by_key = {record['belief_key']: record for record in records}
    if len(by_key) != len(records):
        raise ValueError("Duplicate belief_key in the selection catalog")
    decision = form.model_validate(invoke(records))
    return [by_key[key] for key in dict.fromkeys(decision.belief_keys)], decision.reasoning
