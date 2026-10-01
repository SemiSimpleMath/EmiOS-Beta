"""Selection by each belief's own belief_key, with one bounded validation repair."""
from pydantic import ValidationError


def select_records(records, invoke, form, extra=()):
    """invoke receives the records and optional structured error data; the model answers with the
    belief_key of each record it selects (`form.belief_keys`).

    One identity per record, the belief_key the record itself carries (2026-10-01). Request-local
    S<n> labels beside the belief_key were written back as S<belief number>, failing the routine.
    Callers show the model each record with no other id fields beside the belief_key.

    Provider failures propagate. Invalid model selections receive one correction
    attempt; no fuzzy identity mapping, partial acceptance, or silent omissions.
    `extra` names further belief_key-list fields of `form` (the routine selector's qualifier_keys),
    validated the same way; when given, the result carries them as a third item
    {field: records}, excluding records already selected.
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
            named = [*decision.belief_keys, *(k for field in extra for k in getattr(decision, field))]
            unknown = sorted(set(named) - by_key.keys())
            if unknown:
                raise ValueError(f"belief_key values not in this catalog: {unknown}")
            chosen = [by_key[key] for key in dict.fromkeys(decision.belief_keys)]
            if not extra:
                return chosen, decision.reasoning
            others = {field: [by_key[key] for key in dict.fromkeys(getattr(decision, field))
                              if key not in decision.belief_keys] for field in extra}
            return chosen, decision.reasoning, others
        except (ValidationError, ValueError) as exc:
            if attempt:
                raise ValueError(f"Belief selection invalid after correction: {exc}") from exc
            retry = {'previous_response': raw, 'validation_error': str(exc),
                     'allowed_belief_keys': list(by_key)}
