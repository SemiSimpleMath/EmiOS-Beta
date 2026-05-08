---
name: kg-conflict-triage
description: Combined doctrine + playbook for triaging a reported KG/wiki/card conflict. KG schema invariants, wiki/card projection rules, triage flow, mutation gates, escalation. Use whenever processing a contradiction, stale_content, wiki_contradiction, or duplicate_node finding.
license: Apache-2.0
metadata:
  author: jukka
  version: "2.0"
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
merge them just because labels match (the recurring-event trap).
Same-label Properties on different subjects = correctly separate.
The executor has `kg_merge_nodes`; merges happen on confident
duplicates (label-spacing, alternate spellings) but the human-in-
the-loop dev page review is the gate against the recurring-event
trap, not tool exclusion.

**Available mutator tools (executor's full toolkit).** Use the most
specific one that fits the recommendation:

| Tool | Use for |
|---|---|
| `kg_close_state` | Setting `end_date` on a State or Event |
| `kg_update_node_field` | Surgical edit to one field (date, label, prose) |
| `kg_create_state_node` | Adding a new era under an entity |
| `kg_create_edge`, `kg_delete_edge` | Connecting / removing relationships |
| `kg_rename_label` | When the current label is demonstrably wrong |
| `kg_merge_nodes` | Combining duplicates (label-spacing typos, alternate spellings) — pair with re-pointing edges via update before merging |
| `kg_delete_node` | Removing orphans / fully-superseded nodes |

Every mutation passes `reason` + `finding_id`.

## Wiki + entity card rules

Wiki pages and entity cards both project from the KG.

**Wiki dirty detection** = bullet-text-diff vs `bullet_index` sidecar
(NOT `updated_at`). `description` updates and importance bumps don't
change bullets ⇒ no refresh. `refresh_wiki_page` may silently no-op
under per-page cooldown / importance / nano-critic gates.

**Common wiki-critic false positives** (all → dismiss bucket 3):
- "X takes Y" + "X stopped Y" without checking `end_date`
- Date precision mismatch (`2024-03-15` vs "around 2024")
- Source-relative phrases ("last year") without anchor
- Two same-label States with different dates = distinct eras

**Entity cards.** `regenerate_entity_card` is single-slot — serialize.
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
4. **Otherwise** bucket 1 (mutate) or bucket 2 (regen only).
5. **Act.**
   - Bucket 1: smallest mutation (`kg_close_state` or
     `kg_update_node_field`) → re-read to verify → regen affected
     derivatives → `return_control` with verdict `executed`.
   - Bucket 2: `regenerate_entity_card` / `refresh_wiki_page` →
     verify render changed → `return_control` with verdict
     `executed` and reason "regen-only".
   - Bucket 3: no mutation, no regen → `return_control` with verdict
     `dismissed` and a reason that cites the invariant + node fields,
     readable to the next investigator hitting the pattern.

The verdict goes in your structured final answer. The route layer
applies the finding-status update from the report — you don't write
to `kg_maintenance_finding` directly.

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

Typical: 2-4 reads → bucket → 0-2 mutations → 0-2 regens → resolve →
return_control. **8 actions plenty, 12 cap, do NOT push toward 60.**

## Needs escalation when

- Prose contradicts existing dates and authority unclear.
- Fix needs a non-allowed mutation (merge, delete on connected node,
  rename, create-state-node, create/delete-edge).
- Findings in cluster point opposite directions.
- Blast radius unclear.

`return_control` with verdict `needs_escalation` and a reason that
poses a specific question the user can arbitrate.

## Discipline

Read before mutate. One mutation per step (own kg_revision_log row).
Mutation BEFORE regen. Resolve AFTER regen verifies. Educate in
dismiss reasons. Investigators (wiki critic, state_ttl_estimator,
cluster resolver, wiki connection investigator) propose findings;
they don't mutate — your job includes deciding when their finding is
the schema misread, not the bug.
