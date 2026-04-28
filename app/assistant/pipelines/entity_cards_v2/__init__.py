"""Entity card pipeline v2 — consumes ``kg_projection`` primitives.

In contrast to the v1 accumulator pipeline in
``pipelines/entity_cards/kg_entity_card_pipeline``, v2:

- Uses the shared ``kg_projection`` neighborhood loader, bullet renderer,
  tagger, and tag cache. Wiki and cards share one content-hash tagger cache.
- Does NOT chain batches with a prior-state accumulator. One failed LLM call
  doesn't poison downstream bullets.
- Derives aliases + dedicated contact edges deterministically (no LLM).
- Passes tagged/grouped bullets to a single structured-output LLM call per
  non-empty section.

Layered:
- ``card_inputs``: load + classify data, fully deterministic up to tagging.
- ``contact_extractor``: pull contact_info fields that live on explicit
  contact-typed edges (phone_number, email_address, address, ...).
- ``card_writer``: orchestrate, call the LLM writer, produce final card.
"""
