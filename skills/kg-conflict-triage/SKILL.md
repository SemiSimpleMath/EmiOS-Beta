---
name: kg-conflict-triage
description: Triage a reported KG/wiki/card conflict — diagnose whether the KG is wrong, the derivative is stale, or the reporting agent misunderstood the schema. Use whenever processing a contradiction, stale_content, wiki_contradiction, or duplicate_node finding.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
  applies_when: "kg_resolution_manager (or any agent acting as KG repair surface) processes a contradiction or staleness report"
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

Wiki pages and entity cards are **projections** of the KG. KG is the
single source of truth. A reported conflict is one of:

1. **Real KG issue** — node is wrong (open era should be closed, wrong date, missing edge). Derivative renders correctly from bad data. → Mutate KG first, then regenerate.
2. **Real derivative staleness** — KG is correct, derivative didn't refresh. → Regenerate only.
3. **Investigator misunderstanding** — agent misread the schema (present-tense canonical, validity windows, recurring events, property scoping). KG and derivative both correct. → Dismiss with reason that **educates** the next reader.

Never the fourth — derivative is right, KG is wrong because derivative says so.

## Triage flow

### 1. Read the finding's evidence

Quote the actual disagreeing texts (not the finding's `reason`):
- "Wiki bullet says X"
- "Card key_fact says Y"
- "Two State nodes both claim Z"

### 2. Read the underlying KG nodes

`kg_query` for the State/Event/Property nodes feeding the rendered
content. You need: `id`, `label`, `node_type`, `original_sentence`,
`start_date`, `end_date`, `*_confidence`, `*_prose`. Dates decide the case.

**For mission-critical work** (same-node merges, narrow-precision
timeline calls, suspected extractor misread, near-irreversible
mutations) — also pull the verbatim source via the JOIN ladder in
`resource_kg_principles` ("Walking back to the original conversation
window"). Skip for low-stakes triage.

### 3. Apply the schema invariants

If any of these explains the "contradiction", you're in bucket 3:

- **Present-tense canonical** — "X takes Y" with `end_date != NULL` is a closed era, not a current claim.
- **Recurring events** — same-label, same-subject Events with different dates = separate occurrences.
- **Property scoping** — "duplicate" Property nodes for different subjects are correctly separate.
- **Date precision mismatch** — "around 2024" vs `2024-03-15` is the same fact at different precision.

### 4. Otherwise — bucket 1 or 2

- State is open in KG but prose says ended → bucket 1 (KG wrong). Close State, then regen.
- State already closed in KG, derivative still wrong → bucket 2 (derivative stale). Regen only.
- KG and prose both have dates that disagree → prose is authoritative; mutate to match prose, regen.
- Prose contradicts KG in a way you can't unilaterally resolve → escalate with a sharp question.

### 5. Act

**Bucket 1**: smallest correct mutation (`kg_close_state` or `kg_update_node_field`) → re-read to verify → regenerate affected derivatives → `kg_finding_resolve(action='executed', reason=...)`.

**Bucket 2**: `regenerate_entity_card` and/or `refresh_wiki_page` → verify the new render differs → `kg_finding_resolve(action='executed', reason='regen-only — KG was already correct')`.

**Bucket 3**: `kg_finding_resolve(action='dismissed', reason=<educational note>)`. The reason must:
- Name the schema invariant that resolves the false positive.
- Cite the underlying node's relevant fields (id, end_date, confidence).
- Be readable by the next investigator hitting the same pattern.

Example dismiss reason:
> "Critic flagged 'X takes art lessons' (wiki) vs 'X stopped art lessons in Nov 2025' (prose). State `8a3f2b0c` has `end_date=2025-11-01`, `end_date_confidence=user_set`. Per present-tense canonical, the wiki bullet renders the closed era correctly as a historical claim. Known critic blind spot — the contradiction logic doesn't check end_date."

## Mutation gates

| Tool | Gate |
|---|---|
| `kg_close_state`, `kg_update_node_field` (one field), `kg_create_state_node`, `kg_create_edge`, `kg_delete_edge` | Reversible. Pass `reason` + `finding_id`. |
| `kg_rename_label` | Reversible only when old label demonstrably wrong (typo / bad canonicalization). When prose just clarifies, prefer creating a new node + closing the old. |
| `kg_delete_node` | Only clear orphans with no incoming edges; otherwise escalate. |
| `kg_merge_nodes` | **Banned.** Always escalate (recurring-event trap). |
| `regenerate_entity_card` | Single-slot — serialize. |
| `refresh_wiki_page` | Idempotent; cooldown may absorb. |

## When to escalate

- Prose contradicts existing KG dates and authority is unclear.
- Fix requires a banned mutation (merge, risky delete).
- Findings in the cluster point opposite directions.
- Blast radius spans many entities and is unclear.

`kg_finding_escalate(finding_id, summary, suggested_action, reason)` —
make the summary a specific question the user can arbitrate.

## Stop early — converge, don't over-verify

**After every tool result, ask: "Could I write the verdict (bucket +
cited evidence) RIGHT NOW?"** If yes → `return_control` IMMEDIATELY.

The cost of one more read is real. You've already earned the verdict.
Don't run additional queries "to be thorough" — that's perfectionism,
not rigor.

Two patterns to recognize and stop on:

- **Re-confirming what you already saw.** If your last 2 reads
  surfaced nodes you've already cited, you're rehashing. Return.
- **"Just one more thing to check."** If your verdict has been
  defensible for 3+ cycles and you keep adding queries, you're
  past convergence. Return.

Mission-critical reads (provenance walk for same-node merges,
narrow-precision timeline calls) earn extra cycles — but only on
the SPECIFIC unanswered question, not as a general "keep digging"
mode. Default is converge.

Typical run shape: 2 reads → identify bucket → 0-2 mutations →
0-2 regens → N finding-resolves → return_control. **8 actions is
plenty for most resolutions; 12 is the soft ceiling.** Past 12
without convergence → return_control with partial; do not push
toward 60.

## Discipline

- Read before mutate (one `kg_query` per mutation minimum).
- One mutation per step (own kg_revision_log row).
- Mutation BEFORE regeneration.
- Resolve findings AFTER regen verifies.
- Educate in dismiss reasons — bucket 3 is how you teach the next investigator.
