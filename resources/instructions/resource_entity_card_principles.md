## Entity card principles

Entity card = projection of the KG (`key_facts` + relationships
selected by importance ranking). KG is truth. Same triage as wiki.

**Single-slot generation.** `regenerate_entity_card` serializes —
parallel calls break the DB write path. Run one at a time.

**Three card-staleness patterns:**
| Pattern | Cause | Fix |
|---|---|---|
| Card key_fact contradicts a *closed* State | Card hasn't refreshed | regen only |
| Card key_fact reflects a *still-open* State that prose says ended | KG is wrong | close State first, THEN regen |
| Card missing a recent fact | Card stale, OR new State below importance cut | regen; if writer still excludes, importance ranking decides — don't force |

The middle case is the most common in resolution work — regenerating
without mutating just produces the same wrong card.

**Closed States render past tense** (`used to take art lessons`).
They don't disappear from cards; they shift in tense and rank.
