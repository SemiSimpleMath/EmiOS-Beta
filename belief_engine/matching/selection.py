"""Selection by each belief's own belief_key, with one bounded validation repair."""
from pydantic import ValidationError


def select_records(records, invoke, form):
    """invoke receives the records and optional structured error data; the model answers with the
    belief_key of each record it selects (`form.belief_keys`).

    One identity per record, the belief_key the record itself carries (2026-10-01). Request-local
    S<n> labels beside the belief_key were written back as S<belief number>, failing the routine.
    Callers show the model each record with no other id fields beside the belief_key.

    Provider failures propagate. Invalid model selections receive one correction
    attempt; no fuzzy identity mapping, partial acceptance, or silent omissions.
    """
    if not records:
        return [], "Empty catalog"
    by_key = {record['belief_key']: record for record in records}
    if len(by_key) != len(records):
        raise ValueError("Duplicate belief_key in the selection catalog")
    retry = None
    for attempt in range(2):
        raw = invoke(records, retry)
        try:
            decision = form.model_validate(raw)
            unknown = sorted(set(decision.belief_keys) - by_key.keys())
            if unknown:
                raise ValueError(f"belief_key values not in this catalog: {unknown}")
            return [by_key[key] for key in dict.fromkeys(decision.belief_keys)], decision.reasoning
        except (ValidationError, ValueError) as exc:
            if attempt:
                raise ValueError(f"Belief selection invalid after correction: {exc}") from exc
            retry = {'previous_response': raw, 'validation_error': str(exc),
                     'allowed_belief_keys': list(by_key)}
