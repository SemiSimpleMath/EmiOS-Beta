## KG principles

KG is the source of truth. Wiki + entity cards are projections.

**Node types.** Entity / Concept = timeless identity. State = ongoing
era with validity window (`end_date IS NULL` ⇒ open). Event =
point-in-time / bounded. Goal has `goal_status` column. Property is
subject-scoped (never global).

**Present-tense canonical.** `label` and `original_sentence` are
present-tense BY FORM. Truth-as-of-now lives in `start_date` /
`end_date`. End in past = historical. End null + start set = currently
true. Both null = soft claim. Future start = planned.

**Same-label rules.** Different dates, same person = sequential, both
correct. Recurring same-label Events = separate occurrences (never
merge — `kg_merge_nodes` always escalates per 2026-05-07 trap).
Same-label Properties on different subjects = correctly separate.

**Validity confidence.** `auto_decay` is a guess by `state_decay`,
re-openable. `user_set` / `explicit` are authoritative. `*_prose`
carries fuzzy form when exact dates unknown.

**Mutation classes.**
| Class | Tools |
|---|---|
| Reversible (auto-OK) | `kg_close_state`, `kg_update_node_field` (one field), `kg_finding_resolve`, `kg_finding_escalate` |
| Reversible-with-care | `kg_rename_label` (when label demonstrably wrong) |
| Escalate | `kg_delete_node`, `kg_merge_nodes` (always) |

Every mutation passes `reason` + `finding_id`. `node.description` is a
projection — write via `persist_description`, never directly.

**Data quality red flags** (sanity-check the dates on every cited node):
- `start_date > end_date` (inverted interval)
- Future `start_date` on a past-tense `original_sentence`
- `start_date` exactly equal to `created_at` (defaulted, not observed)

If you find any of these, that's the real KG bug — promote to bucket 1.

**Walking back to the source** (for same-node merges, narrow-precision
timelines, suspected extractor misreads, or near-irreversible mutations):

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

**Investigators (wiki critic, state_ttl_estimator, cluster resolver,
wiki connection investigator) propose findings; they don't mutate.**
Your job includes deciding when a finding is the investigator
misreading the schema rather than a real bug.
