## KG principles — schema invariants

Wiki pages and entity cards are PROJECTIONS of the KG. The KG is the
single source of truth.

### Node types

- **Entity** — identities (people, places, orgs). Timeless; usually no validity dates.
- **State** — ongoing situations with a validity window ("lives in X", "works at Y"). Open era ⇒ `end_date IS NULL`.
- **Event** — point-in-time or bounded occurrences ("got married", "performance"). May have only `start_date`.
- **Goal** — intents. Has `goal_status` column (`active | dormant | completed | abandoned`).
- **Concept** — abstract types ("dog", "marriage"); taxonomy anchors.
- **Property** — subject-scoped attributes (DOB, phone). NEVER global — each anchored to one subject Entity.

### Present-tense canonical (load-bearing)

`label` and `original_sentence` are written present-tense by
`fact_canonicalizer`. **Present tense is FORM, not a claim about NOW.**
Truth-as-of-now lives in `start_date` / `end_date`:

- `end_date IS NULL` → era open, currently true.
- `end_date` in the past → historical era, NOT current.
- Both null → era unknown; soft claim.
- `start_date` in future → planned, not active.

### Validity confidence

`*_confidence`: `explicit | user_set | auto_decay | completed_detected | abandoned_detected | (null)`.
`auto_decay` is a guess by `state_decay`; re-openable if re-observed.

`*_prose` carries natural-language form when exact dates aren't known
(e.g. `start_date_prose: "around 2024"`). Either side may be populated.

### Same-label rules

- Two States, same person, non-overlapping windows = sequential, both correct.
- Recurring same-label Events with different dates = separate occurrences. Never merge. (`kg_merge_nodes` is gated against this — escalate.)
- Two same-label Property nodes for different subjects = correct (Property scoping).

### Mutation reversibility

| Class | Tools |
|---|---|
| Reversible | `kg_close_state`, `kg_update_node_field`, `kg_create_state_node`, `kg_create_edge`, `kg_delete_edge`, `kg_finding_resolve`, `kg_finding_escalate` |
| Reversible-with-care | `kg_rename_label` (when old label demonstrably wrong) |
| Escalate | `kg_delete_node` (unless clear orphan), `kg_merge_nodes` (always) |

Every mutation passes a non-empty `reason` and `finding_id`. Vague reasons are a smell. Reads precede mutates.

### node.description is a projection

Computed from edges + sentence. **Never write `description` directly** — use `persist_description` (preserves `updated_at`).

### Walking back to the original conversation window

For mission-critical work — same-node merge verification, timeline
reconstruction with narrow date precision, "is this an extractor
misread?", near-irreversible mutations — read the verbatim source,
not just the canonicalized fields.

```sql
-- Source message(s) that produced node X
SELECT u.timestamp, u.role, u.speaker_name, u.message,
       e.raw_text, e.observed_at, e.extractor_agent_name
FROM kg_node_evidence ne
JOIN claim_proposal_evidence e ON e.proposal_id = ne.claim_proposal_id
JOIN unified_log_2026 u ON u.id = e.unified_log_id
WHERE ne.node_id = '<node_id>'
ORDER BY u.timestamp ASC;
```

`unified_log_2026.message` is verbatim chat. For emails
(`source='email_inbound'`), the `message` column holds a summary;
full body is in the email pod (`pod_search(kind='email')`).

Trigger window-reading on:
- "Same node?" merges (verify speakers were asserting the same fact)
- Timeline calls hinging on prose-precision dates
- Suspected extractor misread (X's State traces to a message about Y)
- Near-irreversible mutations

Skip for low-stakes triage; this step is for high-stakes only.

### Investigator vs executor separation

Investigators (wiki critic, state_ttl_estimator, cluster resolver,
wiki connection investigator) propose findings; never mutate.
The executor and `kg_resolution_manager` are the only paths from
finding to mutation. Investigators get things wrong sometimes —
your job includes deciding when a finding is the investigator
misunderstanding the schema.
