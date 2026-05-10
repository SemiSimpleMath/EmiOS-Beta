---
name: kg-conflict-triage
description: Combined doctrine + playbook for triaging a reported KG/wiki/card conflict. KG schema invariants, wiki/card projection rules, triage flow, mutation gates, escalation. Use whenever processing a contradiction, stale_content, wiki_contradiction, or duplicate_node finding.
license: Apache-2.0
metadata:
  author: jukka
  version: "2.1"
  applies_when: "kg_resolution_manager (or any agent acting as KG repair surface)"
  auto_inject_when:
    task_keywords:
      - "kg_maintenance_finding"
      - "wiki_contradiction"
      - "stale_content"
      - "duplicate_node"
      - "state_contradiction"
      - "resolve cluster"
      - "fix the kg"
      - "investigate finding"
      - "contradiction"
---

# KG conflict triage

## Meta-principle

**KG is the source of truth. Wiki pages and entity cards are
projections.** A reported conflict can only be:

1. **KG bug** — node wrong (open era should be closed, dates inverted,
   missing edge). Mutate KG → regen → resolve.
2. **Derivative staleness** — KG correct, wiki/card didn't refresh.
   Regen only → resolve.
3. **Investigator misunderstanding** — schema misread (present-tense
   canonical, validity windows, recurring events, property scoping).
   Dismiss with educational reason.

Never the fourth — derivative is right because it says so. KG wins.

## Schema essentials

**Node types.** Entity / Concept = timeless identity. State = ongoing
era with validity window (`end_date IS NULL` ⇒ open). Event =
point-in-time / bounded. Goal has `goal_status` column. Property is
subject-scoped (never global).

**Present-tense canonical.** `label` and `original_sentence` are
present-tense BY FORM. Truth-as-of-now lives in `start_date` /
`end_date`. End in past = historical. End null + start set = open.
Both null = soft claim. Future start = planned.

**Validity confidence.** `auto_decay` is a `state_decay` guess,
re-openable. `user_set` / `explicit` are authoritative. `*_prose`
carries fuzzy form when exact dates unknown.

**Same-label rules.** Different dates, same person = sequential, both
correct. Recurring same-label Events = separate occurrences — do NOT
recommend merging just because labels match (the recurring-event
trap). Same-label Properties on different subjects = correctly
separate.

## Executor capability (what your recommendation can ask for)

The executor can apply any KG mutation (close states, update fields,
merge or delete nodes, add or remove edges, rename labels, create
new states/edges) and refresh derived content (entity cards, wiki
pages). Write recommendations as plain prose that names *what should
change and why*, citing specific node ids and dates — the executor
picks the right tool.

## Wiki + entity card rules

Wiki pages and entity cards both project from the KG.

**Wiki dirty detection** = bullet-text-diff vs `bullet_index` sidecar
(NOT `updated_at`). `description` updates and importance bumps don't
change bullets ⇒ no refresh.

**Common wiki-critic false positives** (all → dismiss bucket 3):
- "X takes Y" + "X stopped Y" without checking `end_date`
- Date precision mismatch (`2024-03-15` vs "around 2024")
- Source-relative phrases ("last year") without anchor
- Two same-label States with different dates = distinct eras

**Entity cards.** Card regeneration is single-slot — serialize.
Three card-staleness patterns:

| Pattern | Cause | Fix |
|---|---|---|
| Card key_fact contradicts a *closed* State | Card hasn't refreshed | regen only |
| Card reflects *still-open* State that prose says ended | KG wrong | close State first, THEN regen |
| Missing recent fact | Stale OR below importance cut | regen; importance ranking decides |

Closed States render past tense, don't disappear from cards.

**Order:** mutate KG first, regen second. Don't blanket-refresh
neighbors — only entities whose bullets actually used the changed fact.

## Data quality red flags

**Sanity-check dates on every cited node.** Common KG bugs that look
superficially valid:

- `start_date > end_date` (inverted interval — usually a legacy
  import where a default-now timestamp got written)
- Future `start_date` on a past-tense `original_sentence`
- `start_date` exactly equal to `created_at` (defaulted, not observed)

If you find any of these, that's the real KG bug — promote to bucket 1
regardless of what the original investigator was reporting.

## Triage flow

1. **Read the finding's actual disagreeing texts** (not just `reason`).
2. **`kg_query` the cited nodes**: `id`, `label`, `original_sentence`,
   `start_date`, `end_date`, `*_confidence`, `*_prose`, `created_at`.
   Sanity-check the dates per "Data quality red flags".
3. **Apply schema invariants.** If any explains the contradiction
   (present-tense + closed era / recurring events / property scoping
   / date precision mismatch) → bucket 3.
4. **Otherwise** bucket 1 (KG bug, recommend a mutation) or
   bucket 2 (regen only).
5. **Write the verdict.**
   - Bucket 1: name the smallest change that fixes it (close this
     state, edit this date, merge these two ids, etc.) — the
     executor operationalizes.
   - Bucket 2: recommend regen of the affected card or wiki page.
   - Bucket 3: dismiss with a reason citing the invariant + node
     fields, readable to the next investigator hitting the pattern.

## Walking back to source messages (for high-stakes calls)

For same-node merges, narrow-precision timelines, suspected extractor
misreads, near-irreversible mutations — read the verbatim source via
SQL JOIN:

```sql
SELECT u.timestamp, u.role, u.speaker_name, u.message,
       e.raw_text, e.observed_at, e.extractor_agent_name
FROM kg_node_evidence ne
JOIN claim_proposal_evidence e ON e.proposal_id = ne.claim_proposal_id
JOIN unified_log_2026 u ON u.id = e.unified_log_id
WHERE ne.node_id = '<node_id>'
ORDER BY u.timestamp ASC;
```

Skip for low-stakes triage.

## Stop early

After every tool result: **"Could I write the verdict (bucket + cited
evidence) NOW, AND have I sanity-checked dates on every cited node?"**
Both yes → `return_control` immediately.

Convergence is NOT just "I have a plausible answer." It's "I've ruled
out data-quality bugs on the cited nodes." Bugs hide on the OTHER
nodes you didn't sanity-check.

Stop signals: rehashing nodes you've already cited / "just one more
thing to check" / verdict has been defensible 3+ cycles.

Typical: 2-4 reads → bucket → resolve. **8 actions plenty, 12 cap, do
NOT push toward 60.**

## Needs escalation when

- Prose contradicts existing dates and authority unclear.
- The fix would require a near-irreversible mutation the user should
  approve (merging two nodes that both have rich evidence; deleting
  a connected hub).
- Findings in a cluster point opposite directions.
- Blast radius unclear.

`return_control` with verdict `needs_escalation` and a reason that
poses a specific question the user can arbitrate.

## Discipline

Read before recommending. One mutation per recommendation step
(own `kg_revision_log` row). Mutation BEFORE regen. Educate in
dismiss reasons. Investigators (wiki critic, state_ttl_estimator,
cluster resolver, wiki connection investigator) propose findings;
they don't mutate — your job includes deciding when their finding is
the schema misread, not the bug.
