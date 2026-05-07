## Entity card principles — derived from KG

Entity cards render a curated summary (`key_facts` + relationships)
of an entity's most important States/Events/Properties. Cards are
projections; the KG is truth. Same triage flow as wiki pages.

### Pipeline

The entity_cards pipeline ranks an entity's neighbors by importance
and feeds them to `card_writer`, which produces `key_facts` and a
short prose summary. Output lands in the `entity_cards` table.

### Single-slot generation

`regenerate_entity_card` is **single-slot** — bulk, single, scheduled
all share one in-flight slot. The DB write path breaks under parallel
writers. Serialize regenerations; do them one at a time.

### Three card-staleness patterns

| Pattern | Cause | Fix |
|---|---|---|
| Card key_fact contradicts a *closed* State | Card hasn't refreshed since closure | `regenerate_entity_card` only |
| Card key_fact reflects a *still-open* State that prose says ended | KG is wrong | Close State first, THEN regen |
| Card missing a recent fact | Card hasn't refreshed; OR new State below importance cut | Regen; if writer still excludes it after regen, importance-rank decides — don't force it |

The middle case is the most common in resolution work: regenerating
without mutating just produces the same wrong card.

### Cards render past tense for closed States

When a State has `end_date != NULL`, the writer renders past form
("she used to take art lessons"). Closed States don't disappear from
the card — they shift in tense and rank.

### Stale-content findings

`step_stale_content_scan` heuristic: card.updated_at vs
node.updated_at delta > N days. Heuristic, not proof. Verify by
re-rendering and comparing key_facts before treating as a real bug.

### Don't blanket-refresh

A single State change usually only affects 1-2 cards (the entity it's
attached to + the other endpoint of any cross-card edge). Don't
regenerate every card mentioning the entity.
