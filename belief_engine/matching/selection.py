"""Exact request-local selection identities and one bounded validation repair."""
from pydantic import ValidationError


def select_records(records, invoke, form):
    """invoke receives complete labeled records and optional structured error data.

    Provider failures propagate. Invalid model selections receive one correction
    attempt; no fuzzy identity mapping, partial acceptance, or silent omissions.
    """
    if not records:
        return [], "Empty catalog"
    labeled = [{**record, 'selection_id': f'B{i}'} for i, record in enumerate(records)]
    by_id = {record['selection_id']: original for record, original in zip(labeled, records)}
    retry = None
    for attempt in range(2):
        raw = invoke(labeled, retry)
        try:
            decision = form.model_validate(raw)
            unknown = sorted(set(decision.belief_ids) - by_id.keys())
            if unknown:
                raise ValueError(f"Unknown selected belief IDs: {unknown}")
            return [by_id[key] for key in dict.fromkeys(decision.belief_ids)], decision.reasoning
        except (ValidationError, ValueError) as exc:
            if attempt:
                raise ValueError(f"Belief selection invalid after correction: {exc}") from exc
            retry = {'previous_response': raw, 'validation_error': str(exc),
                     'allowed_ids': list(by_id)}
