# KG Health Components — full inventory

Canonical reference of every component involved in maintaining the
knowledge graph + its derivatives (wiki pages, entity cards). When a
new finding shows up, this is the doc you want open.

This is the **canonical** description of the self-healing loop as of
2026-06-16. For the typed mutator tools that actually write to the KG
(handler inventory, FK-cascade trap, `kg_revision_log` schema,
cookbooks for adding new ops or producers) see
`13_KG_MUTATOR_TOOLS.md`.

## The self-healing loop in one paragraph

Producers (wiki critic, structural scans, decay jobs) write
**findings** describing suspected problems. The **investigator**
(`kg_investigation_manager`) is read-only — it queries the live KG via
`kg_query`, walks provenance, and emits a structured report whose
load-bearing field is `take_action: bool`. If `False`, the finding
closes immediately as `dismissed`, and the investigator's verdict
(memo + type + node_ids) is recorded in `kg_node_verdict` so future
runs don't re-investigate the same question. If `True`, the finding
goes either to a 24-hour dev-page grace window (`disposition='auto_apply'`)
or to the user-review queue (`disposition='needs_user_review'`). After
the grace window expires (or a human clicks Accept), the **executor**
(`kg_resolution_manager`) reads the recommendation prose and applies
the corresponding KG mutations using its full mutator toolkit. Every
mutation lands in `kg_revision_log` with before/after snapshots.

## Daily timeline (cron, local times)

Schedules are the `run_policy` blocks in `configs/routines/public/*.json`
unless noted. `kg_pipeline` is the exception — it has **no** `run_policy`;
its `trigger` is `{type: time, policy: {daily, time_local: 23:00},
active_window: kg_active}`, i.e. fire at 23:00 inside the `kg_active`
window (`configs/windows.json`). Tune the window, not `time_local`.

```
DAILY (every night):
23:00  kg_pipeline (pipeline, trigger+active_window) ← INGEST: chat → claim_proposals (fires at start of kg_active)
02:30  proposal_promoter (function)         ← MERGE BOT: promote claim_proposals to live KG nodes/edges
02:45  kg_state_decay (function)            ← auto-close stale States (writes findings as 'executed' — skips investigator)
02:50  kg_goal_dormancy_sweep (function)    ← active → dormant
02:55  kg_goal_outcome_detect (function)    ← "I finished X" → terminal close (limit 10/day)
03:00  kg_finding_backlog_drain (function)  ← INVESTIGATE: 20/day, excludes duplicate_node/state_missing_dates/synthetic_fact_proposal
03:00  wiki_nightly_refresh (function)      ← regen wiki pages whose bullets changed + consistency critic
03:15  kg_state_date_drain (function)       ← INVESTIGATE state_missing_dates findings (8/day, specialized brief)
03:15  kg_dup_cluster_drain (function)      ← adjudicate duplicate_node clusters (union-find → 1 LLM call/cluster, 50/run)
03:30  kg_finding_cluster_resolve (function)← group N redundant findings into 1 lead (30 clusters/run)
03:30  wiki_growth (function)               ← write new wiki pages for high-degree entities
03:45  kg_identity_sentence_refresh (function) ← (re)gen graph-derived identity sentences → node_identity_embeddings (≤1500/run)
04:00  kg_evidence_digestion (function)     ← fold oldest evidence rows of heaviest-evidenced nodes into a digest (5 nodes/night)
04:15  kg_wiki_inference (function)         ← propose new edges from wiki page prose → claim_proposals
04:30  entity_card_refresh (function)       ← rebuild ≤10 stale + build ≤5 new cards (7d/card cooldown, card_critic veto), deactivate orphans
09:00  kg_date_gap_drain (function)         ← enqueue 2 state_missing_dates findings as pending questions (7d/finding cooldown)

INTERVAL:
every 15 min  kg_finding_executor_drain (function) ← EXECUTE: 10/run; auto_apply findings whose 24h grace expired (+ user-Accepted)
every 30 min  kg_importance_rater (function)       ← edge LLM rater (≤400 edges/run); node importance DERIVED
every 60 min  kg_embedding_diff_sync (function)    ← reconcile chroma label/identity/context vs sqlite (≤1200 embeds/run)
daily (24h, 08:00–22:00 window)  wiki_fact_drain (function) ← confirm synthetic_fact_proposal facts as questions (≤2/run)

WEEKLY:
Mon 02:00  kg_maintenance_pipeline (pipeline)  ← heavy audit (step order below)
Sun 04:10  kg_embedding_full_rebuild (function)← re-embed every node's label+identity+context (belt-and-suspenders floor)
```

There is **no** `entity_cards` pipeline routine and **no**
`entity_card_maintenance_pipeline` — the only card routine is
`entity_card_refresh` (04:30). The old "generate pipeline + audit
pipeline + card-findings table" design was removed; card QA is now a
single L0 `card_critic` veto inside the nightly refresh (see below).

## A failed call stops processing (2026-10-05)

Owner, 2026-10-05: "If any call fails in KG, we should treat that as a
stop processing." Every component in this doc follows the same rules:

- **A failed LLM, embedding, Chroma or DB call raises and the run
  stops.** The item in hand and everything after it stay pending for the
  next run. Nothing is skipped, defaulted, passed through, stamped as
  scanned or recorded as a verdict because a call failed (an outage is
  never a verdict). Result dicts no longer carry `errors` / `failed`
  counts; a run either finishes or raises.
- **A well-formed but incomplete answer gets ONE targeted correction**,
  then raises. The re-ask names exactly what is missing or malformed (the
  omitted ids, the invalid verdicts); the agents gained a `correction`
  slot in their user prompt for it (the edge canon curator receives it in
  its `information` field). An answer with no structured data at all (no
  verdict list) raises without a correction.
- **Per-item failure ledger** (`app/assistant/kg/failure_ledger.py`,
  table `kg_item_failure`). A queue loop records each failure against the
  item it concerns (`record_failure(kind, item_id, exc)`) and re-raises;
  a success clears the count. After `SET_ASIDE_AFTER = 3` failed runs the
  item is **set aside**: its queue skips it (`set_aside_ids(kind)`) and a
  `processing_failure` finding, born `escalated`, puts it in front of the
  owner. A set-aside item is never dropped; `release(kind, item_id)`
  returns it to its queue. This is what stops one item that always fails
  (a content refusal, an over-long prompt) from blocking its queue every
  night.

Ledger kinds and the queue that uses each:

| Kind | Item | Queue |
|---|---|---|
| `kg_window`, `kg_extraction`, `kg_enrichment` | window / extraction / enrichment | ingestion (`09_KG_PIPELINE.md`) |
| `kg_proposal` | claim proposal | `run_promoter` |
| `edge_importance` | edge | `kg_importance_rater` edge rater |
| `section_tag` | node | `kg_importance_rater` section tagger |
| `entity_card` | entity node | `entity_card_refresh` |
| `kg_finding_investigation` | finding | `investigate_findings` (backlog / state-date drains, pipeline final pass, wiki critic) |
| `kg_finding_execution` | finding | `run_executable_findings` |
| `kg_duplicate_cluster` | cluster (sha1 of its node-id set) | `kg_dup_cluster_drain` |
| `kg_duplicate_pair` | candidate pair (`min_id\|max_id`) | `duplicate_scan` detector |
| `kg_role_succession` | Entity node | `role_succession_scan` |
| `kg_wiki_inference` | page subject node | `kg_wiki_inference` |
| `kg_finding_cluster` | primary node id | `kg_finding_cluster_resolve` |
| `kg_context_embedding`, `kg_label_embedding` | node | `context_embedding_backfill` |
| `kg_identity_sentence` | node | `kg_identity_sentence_refresh` |
| `kg_evidence_digest` | node | `kg_evidence_digestion` |

Per-component specifics are in the sections below and in
`09_KG_PIPELINE.md` (ingestion, promoter), `12_ENTITY_CARDS.md`,
`23_NODE_IMPORTANCE.md` and `13_KG_MUTATOR_TOOLS.md` (Chroma cleanup after
a committed merge/delete raises naming the revision).

## Lifecycle of a finding

```
                      producer.upsert_finding()
                              │
                              ▼
                       ┌──────────────┐
                       │  pending     │
                       └──┬───────────┘
                          │
        ┌─────────────────┴────────────────────┐
        │ kg_finding_backlog_drain             │   step_state_decay
        │   → kg_investigation_manager         │   writes directly with
        │                                      │   initial_status='executed'
        ▼                                      ▼   (state_auto_closed bypass)
 ┌────────────────────────────┐         ┌──────────┐
 │ investigator's report      │         │ executed │
 │ written to                 │         └──────────┘
 │ investigation_report_json  │
 └──────┬─────────────────────┘
        │
        │  take_action=False?           take_action=True?
        │           │                            │
        │           ▼                            ▼
        │   ┌──────────────┐         ┌──────────────────────┐
        │   │ verdict_type │         │ disposition routing  │
        │   │ verdict_memo │         │  auto_apply →        │
        │   │ verdict_     │         │   24h grace, then    │
        │   │   node_ids   │         │   executor (Path A)  │
        │   │  recorded in │         │  needs_user_review → │
        │   │  kg_node_    │         │   user queue (no     │
        │   │  verdict     │         │   timer)             │
        │   └──────┬───────┘         └──────────┬───────────┘
        │          │                            │
        │          ▼                            ▼
        │   ┌──────────────┐         ┌──────────────────────┐
        │   │  dismissed   │         │ kg_finding_executor_ │
        │   │  (terminal)  │         │ drain →              │
        │   └──────────────┘         │ kg_resolution_       │
        │                            │ manager applies      │
        │                            └──────────┬───────────┘
        │                                       │
        │                          ┌────────────┴───────────┐
        │                          ▼                        ▼
        │                  ┌──────────────┐         ┌──────────────┐
        │                  │  executed    │         │  escalated   │
        │                  │  (mutations  │         │  (no-op:     │
        │                  │   applied)   │         │   planner    │
        │                  └──────────────┘         │   declined   │
        │                                           │   to act)    │
        │                                           └──────────────┘
        │
        │ user opens dev page (`/kg-maintenance/investigated`):
        │   – edits recommendation textarea (autosaves on blur)
        │   – adds operator notes (autosaves on blur)
        │   – clicks Accept → executor runs immediately, bypasses 24h gate
        │   – clicks Decline → status flips to dismissed
```

`superseded_by` is **orthogonal** to this state machine: when
`kg_finding_cluster_resolver` distills a cluster, siblings get
`superseded_by = lead.id` set and the dev pages hide them by default.
The lead still cycles through pending → investigated → terminal.

## Status vocabulary

| Status | Reached by |
|---|---|
| `pending` | producer wrote it |
| `investigated` | investigator's report written; awaiting executor or user |
| `dismissed` | investigator emitted `take_action=False`; verdict preserved in `kg_node_verdict`. Terminal. |
| `executed` | executor applied mutations (or `step_state_decay` short-circuit). Terminal. |
| `escalated` | executor's planner declined to operationalize the recommendation (data shifted, ids stale, etc.). Terminal — re-investigate via `/kg-dev` if needed. |

`approved` and `rejected` exist in the schema but are no longer in active use — they were the old human-in-the-loop states before the dev-page workflow.

## Investigator: `kg_investigation_manager`

Manager config: `app/assistant/multi_agents/kg_investigation_manager/config.yaml`. Read-only enforcement is two-layer:

1. `tools.allowed_tools` and `scope_contract.tools.allowed_tools` list **only** `kg_query`. Curated down from a larger set in 2026-05-07; the investigator earns its budget through SQL, not pod search.
2. `scope_contract.writes.write_kg: false` — even if a tool slipped through, the scope context refuses authorization.

**Planner agent** (`kg_investigation::planner`, `gpt-5.4` powerful tier):
- Statically binds the `kg-conflict-triage` skill (combined triage doctrine + playbook).
- `entity_card_level: 0` — only one-liner identity, not the disputed key_facts. Avoids priming the investigator with the very claims under investigation.
- System prompt teaches: one question per query, counts before bodies, stop early, walk provenance, don't speculate beyond the data.

**Final-answer schema** (`agent_form.AgentForm`):

```python
take_action: bool                    # ★ load-bearing
recommendation: str                  # prose plan (or verdict reasoning)
disposition: str                     # 'auto_apply' | 'needs_user_review' (when take_action=True)
user_question: Optional[str]         # required when disposition='needs_user_review'
verdict_type: Optional[str]          # required when take_action=False
verdict_memo: Optional[str]          # required when take_action=False
verdict_node_ids: List[str]          # required when take_action=False (1-3 ids)
confidence: float
diagnosis: str
evidence: List[EvidenceItem]
open_questions: List[str]
final_answer_answer: str
result_summary: str
```

**Verdict vocabulary** (when `take_action=False`):

| `verdict_type` | Meaning | Example memo |
|---|---|---|
| `distinct` | Two named nodes are NOT the same thing (pairwise) | `"do not merge 42bc6a1b with 929ee949 — concept vs household pets"` |
| `verified` | Single-node data is correct as-is | `"start_date 2024-09-01 on 8a3f confirmed by unified_log:5678"` |
| `false_positive` | Flagged issue isn't real | `"wiki_contradiction false positive — closed-era past tense"` |
| `obsolete` | Finding refers to since-superseded data | `"node already merged 2026-05-04"` |
| `irrelevant` | Finding type doesn't apply to this case | `"state_missing_dates rule mis-fires on Properties"` |

The memo is `~12 words`, names node ids inline, and persists in `kg_node_verdict` for future agents to consult.

**Failed investigations** (2026-10-05). A failed or aborted manager run
(`invoke_manager`, see the executor section), or a run that ends without
a structured report, raises and leaves the finding `pending`.
`investigate_findings` stops at the first failure and counts it in the
failure ledger (kind `kg_finding_investigation`); `drain_pending_findings`
and `run_pending_findings` both delegate to it, and set-aside findings are
skipped. The `evidence_json.investigation_attempts` counter and its
escalation after three failed attempts are retired: the ledger now counts
per item, and a set-aside finding surfaces as an escalated
`processing_failure` finding instead of being escalated itself. The
finding brief (`finding_brief.build_finding_brief`) and the importance
ordering (`finding_priority`) raise on a failed read instead of producing
a thinner brief or a zero score. Results carry `processed` /
`investigated`, no `errors`.

## Executor: `kg_resolution_manager`

Manager config: `app/assistant/multi_agents/kg_resolution_manager/config.yaml`. Same `MultiAgentManager` shell. Reads the investigator's prose recommendation and applies the corresponding mutations.

**Planner agent** (`kg_resolution::planner`, `gpt-5.5` smart tier):
- Statically binds the `kg-conflict-triage` skill (same doctrine as the investigator).
- `entity_card_level: 2` (entity context for mutation reasoning).
- Full toolkit — 10 mutators + 2 finding-lifecycle ops. The manager
  config (`tools.allowed_tools`) grants all 12; the planner's own
  `allowed_tools` lists 8 mutators (it leans on the finding-lifecycle
  ops and the newer disambiguation ops being visible at the manager level):

| Tool | Purpose |
|---|---|
| `kg_query` | Read-only SELECT/WITH (one statement, 200-row default cap) |
| `kg_close_state` | Set `end_date` on a State or Event |
| `kg_create_state_node` | Add a new era under an entity |
| `kg_update_node_field` | Surgical edit to one field (date, label, prose, etc.) |
| `kg_rename_label` | When the current label is demonstrably wrong |
| `kg_create_edge` / `kg_delete_edge` | Connecting/removing relationships |
| `kg_repoint_edge` | Move one edge endpoint (Disambiguation drain) |
| `kg_split_succession` | Era-split a role-reference entity (close old era, mint successor + Disambiguation, chain `succeeded_by`) |
| `kg_delete_node` | Remove orphans / fully-superseded nodes |
| `kg_merge_nodes` | Combine duplicates (label-spacing typos, alternate spellings) |
| `kg_finding_resolve` | Close a finding `executed` (finding lifecycle) |
| `kg_finding_escalate` | Route a finding to `escalated` (finding lifecycle) |

**Safety model**: every executor invocation is gated by either the
24-hour dev-page grace window (auto_apply path) or an explicit human
Accept (override path). The recurring-event-trap protection lives in
the *human review*, not in tool exclusion. The investigator's own
prompt biases toward `needs_user_review` for ambiguous merges, and the
dev page surfaces the recommendation prose for audit before mutations
land.

The driver (`finding_executor.execute_one`) detects no-op runs (manager
returned without mutations or regenerations) and flips the finding to
`escalated` rather than leaving it stuck at `investigated`.

**A failed run is never an outcome** (2026-10-05). Manager runs go
through `finding_processor.invoke_manager`, which raises on an exception,
a missing manager, or a run `ManagerInvoker` returns as `manager_aborted`
(an abort does not raise, and its graceful-exit final answer looked like a
report). Any failure after the claim, including an abort after partial
mutations and a run that ends without a `kg_resolution::final_answer`,
**releases the claim** (back to `investigated`) and raises, so an outage
is never recorded as `executed` or `escalated`.
`run_executable_findings` stops at the first failed finding (ledger kind
`kg_finding_execution`); the rest stay `investigated`. Its result carries
`executed` / `escalated` / `skipped`, no `errors`. The legacy `apply_one`
(`kg_mutation_manager`) also raises on a failed or aborted run.

### Scope-context trick

The system refuses to widen `writes.write_kg` from `False` to `True`
via `scope_contract` — that path can only narrow, never grant. So
`finding_executor._executor_scope` builds a permissive scope and
stamps it on the inbound `Message`:

```python
ScopeContext(
    scope_id="scope::kg_investigator::finding_executor",
    owner_id="jukka",
    actor_id="kg_finding_executor",
    surface="system",
    approval=ScopeApprovalPolicy(authority_level=100),
    resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    writes=ScopeWritePolicy(write_kg=True, write_unified_log=True),
)
```

The manager's own `scope_contract` then narrows the tool surface to
its curated mutator allowlist. Net effect: the manager can write to the
KG, but only via the typed tools listed above.

## The `kg_node_verdict` table

`app/assistant/database/kg_node_verdict.py`. Durable record of "we
already decided about this." Pairwise verdicts canonicalize on
(`node_id_a < node_id_b`); single-node verdicts use `node_id_a` only
with `node_id_b` NULL.

| Column | Role |
|---|---|
| `id` | uuid |
| `node_id_a`, `node_id_b` | canonical-ordered subjects |
| `verdict_type` | controlled vocabulary (see above) |
| `memo` | the investigator's short imperative line |
| `reasoning` | the longer-form `recommendation` prose |
| `source_finding_id` | back-pointer to the finding that produced it |
| `decided_by` | agent name (or `'user'` for human-overridden verdicts) |
| `confidence` | investigator's confidence |
| `superseded_at`, `superseded_reason` | soft-delete (verdict overridden by a later investigation) |
| `created_at` | aware-UTC timestamp |

Helper module `app/assistant/kg_maintenance/verdict_store.py` exposes:

- `record_verdict(...)` — write (auto-canonicalizes pair order)
- `get_verdicts_for_pair(a, b)` — order-insensitive pair lookup
- `get_verdicts_for_node(id)` — any verdict touching this node
- `is_pair_marked_distinct(a, b)` — predicate for filters
- `supersede_verdict(id, reason)` — soft-delete

### Three readers

1. **`step_duplicate_scan._add_pair`** — drops candidate pairs with a prior `'distinct'` verdict before the LLM duplicate-detector runs (logs `prior_verdict_skipped=N` per run). Closes the loop: next month's duplicate scan won't re-flag the same pair.
2. **`finding_brief.build_finding_brief`** — every brief gets a `## Prior verdicts on these nodes` section listing what was decided before, so the investigator can confirm/supersede instead of re-deriving.
3. **`proposal_promoter` merge filter** — *implemented* (`proposal_promoter._maybe_merge_*` consults `verdict_store.load_distinct_verdicts_among(cand_ids)`). When the maintenance loop already ruled two of the merge candidates `'distinct'` from each other, the promoter **memo-injects** that fact into the `node_merger` agent's candidate payload (`prior_distinct_verdicts`) rather than dropping the pair. Rationale: the verdict says the *candidates* differ from each other, not which (if either) matches the new observation — deterministic proposes, the LLM decides. Closes the loop earlier than the duplicate scan: a new observation can't be re-bound into the wrong twin.

## Component tables

### Producers (write findings)

Maintenance-pipeline step order (`kg_maintenance_pipeline/pipeline.py`,
run Mon 02:00): `lifecycle_gc` → `orphan_scan` →
`context_embedding_backfill` → `duplicate_scan` → `pagerank` →
`missing_dates_scan` → `edge_canon_curation` → `arrangement_scan` → `role_succession_scan` →
`date_lint_scan` → `disambiguation_scan` → `investigate_findings`
(final pass, caps at 20 investigations/run). **A failed step stops the
run** (2026-10-05): the remaining steps do not run and the step's
exception propagates, so the routine layer records the run as an error
(backoff / auto-disable / ticket); each step leaves its unfinished work
pending for the next run. Until then a failed step was recorded as
`{"error": …}` in the summary (status `error`, `failed_steps`) and the
pipeline went on to the next step. Inside the steps, the rules in "A failed call stops
processing" apply; step specifics are in the per-step failure table below.
`lifecycle_gc` and
`edge_canon_curation` landed in the 2026-07-08 KG-core audit (deletion
lifecycle + predicate canon curation). The old
`description_fill`/`missing_description` step was deleted 2026-06-10
(wiki page leads are the authoritative source of `node.description`).

| Producer | Type | finding_type | Notes |
|---|---|---|---|
| `step_orphan_scan` | maintenance pipeline step | `orphan_node` | Cheap structural scan |
| `step_duplicate_scan` | maintenance pipeline step | `duplicate_node` | Three-tier candidate gen + LLM confirm; consults `kg_node_verdict` to skip already-decided pairs; a failed call stops the run before any node is stamped (see the failure table below). Drained by `kg_dup_cluster_drain`, not the backlog drain |
| `step_missing_dates_scan` | maintenance pipeline step | `state_missing_dates` | State/Event nodes missing one or both dates, scored by connected-entity pagerank |
| `step_arrangement_scan` | maintenance pipeline step | (arrangement supersession, 2026-10-05) | Pairs each open State with later-said States/Events sharing a participant (not the owner; said dates from the source messages), ranked by sentence similarity; `kg_maintenance::arrangement_judge` says supersedes / both_true / same_statement / unrelated / unsure. Supersedes becomes an `arrangement_supersession` finding born investigated / needs_user_review (Accept closes the State and adds `succeeded_by`); a State is re-judged only when a new later statement appears. ≤100 per run, freshest changes first |
| `step_role_succession_scan` | maintenance pipeline step | (diachronic disambiguation) | Judges role-reference entities for referent changes before the disambiguation scan |
| `step_date_lint_scan` | maintenance pipeline step | date-quality findings | Lints date intervals (e.g. inverted start/end) |
| `step_disambiguation_scan` | maintenance pipeline step | (Disambiguation drain) | Retires a marker whose label is back to one referent (re-points every parked edge to it, rebinds its evidence, deletes it; added 2026-10-05), then a temporal drain re-points mentions parked at split labels |
| `step_state_decay` | daily routine | `state_auto_closed` | Closes stale States, writes finding with `initial_status='executed'` so it skips the investigator |
| `wiki_consistency_critic` | wiki-pipeline agent | `wiki_contradiction` | Bypasses `upsert_finding` — writes rich `evidence_json` directly |
| `wiki_connection_investigator` | `kg_wiki_inference` routine | `synthetic_fact_proposal` | Investigator handles duplicate-check; promotion path is `synthetic_fact_review` |

**Per-step failure behaviour** (2026-10-05; each raise stops the step and,
inside the weekly pipeline, the run):

| Step / helper | On a failed call | On an incomplete answer |
|---|---|---|
| `lifecycle_gc` | A failed Chroma edge-ghost sweep raises (the SQL sweep before it is already committed; the next run retries) | — |
| `context_embedding_backfill` | A failed embed or Chroma write raises; ledger kinds `kg_context_embedding` / `kg_label_embedding`; set-aside nodes skipped; result has no `errors` key | — |
| `duplicate_scan` | A failed triage batch, Tier 4 load, Tier 5b Chroma read or detector call raises before any node is stamped scanned (no pass-through of a failed triage batch; no stamping after a failed tier). A failed detector call is counted against the pair (kind `kg_duplicate_pair`); a set-aside pair is not re-sent, and its nodes stay **unstamped** so a released pair returns | Triage batch: a pair without a valid verdict (`distinct` / `uncertain` / `defective_sentences`) gets one correction, then raises. Detector: no `merge_actions` list raises |
| `missing_dates_scan` | An unconfigured primary user name raises | — |
| `edge_canon_curation` | Every batch is judged before anything is applied: a missing agent or failed call raises with nothing written; a failed alias-cache reset raises | A predicate without a valid verdict, or a `variant_of` naming no existing canon, gets one correction, then raises |
| `role_succession_scan` | A failed judge call raises; kind `kg_role_succession` | A malformed verdict (verdict, confidence, a succession's `split_date` / `split_basis`) gets one correction, then raises; it never becomes a cooldown finding |
| `disambiguation_scan` | A failed temporal-drain transaction raises; the rest stays parked | — |
| `investigate_findings` (final pass) | See the investigator section | — |
| `kg_finding_cluster_resolve` (`step_cluster_resolve`) | A failed call raises; kind `kg_finding_cluster` (keyed by primary node) | A "same root" answer missing its lead, members or root question, or naming foreign finding ids, gets one correction, then raises |
| `kg_goal_outcome_detect` | A failed call or evidence read raises; every Goal stays as it is | A candidate without a valid outcome / numeric confidence, or an unknown id, gets one correction, then raises |
| `kg_wiki_inference` | A failed call, page read or finding write raises before the page is marked examined, so it is read again next run; kind `kg_wiki_inference` | No `sentences` list raises |
| `kg_identity_sentence_refresh` | A failed writer call or source-window read raises before anything is stamped (no `undefinable` stamp after a failed read); kind `kg_identity_sentence`. A failed embed or Chroma write after the sentence is written raises too (the hourly sync heals the vectors). An unconfigured primary user name raises | `definable` with no sentence gets one correction, then raises |
| `kg_evidence_digestion` | A failed digester call raises with nothing written for that node; kind `kg_evidence_digest` | An empty digest gets one correction, then raises |
| Approved-finding executors (`step_execute_findings`, run from `/kg-maintenance`) | `duplicate_node`: a failed or empty `node_data_merger` answer raises before anything is written; the field-copy fallback merge is gone. The batch route `/api/execute_approved` stops at the first failure (that finding marked `execute_error`, retryable; the rest stay `approved`). The unwired autopilot `run()` was deleted | — |

### Cron functions (drains + audits)

| Routine | Function | Manager invoked |
|---|---|---|
| `kg_finding_backlog_drain` | `finding_processor.drain_pending_findings` | `kg_investigation_manager` |
| `kg_state_date_drain` | `finding_processor.run_pending_findings(finding_types=["state_missing_dates"])` | `kg_investigation_manager` (specialized brief) |
| `kg_dup_cluster_drain` | `duplicate_cluster_drain.drain_duplicate_clusters` | `kg_maintenance::duplicate_cluster_resolver` agent (see below) |
| `kg_finding_executor_drain` | `finding_executor.run_executable_findings` | `kg_resolution_manager` (only `auto_apply` findings past 24h grace) |
| `kg_finding_cluster_resolve` | `step_cluster_resolve.run` | `kg_finding_cluster_resolver` agent |
| `kg_state_decay` | `step_state_decay.run` | (none — pure SQL; writes `state_auto_closed` as `executed`) |
| `kg_goal_dormancy_sweep` | `step_goal_dormancy.run` | (none) |
| `kg_goal_outcome_detect` | `step_goal_outcome_detect.run` | `goal_outcome_detector` agent (one-shot) |
| `kg_wiki_inference` | `step_wiki_inference.run` | `wiki_connection_investigator` agent → claim_proposals |
| `kg_importance_rater` | rater function | (none) |

**Duplicate cluster drain** (`kg_investigator/duplicate_cluster_drain.py`).
Union-finds pending `duplicate_node` findings into clusters and asks
`kg_maintenance::duplicate_cluster_resolver` once per cluster to place
every node id exactly once, in a merge group or in `distinct_node_ids`.
A node the answer leaves out is **never taken as `distinct`** (2026-10-05):
an answer that omits, repeats or invents ids, or has a merge group with no
duplicates, gets one correction naming the problems (the agent's
`correction` slot), then raises. All of a cluster's merges commit in **one
transaction**; any failure, including a merge winner that no longer
exists, rolls the whole cluster back and leaves its findings pending. Only
after that commit are the findings closed (`executed` for pairs that
collapsed, `dismissed` + recorded `distinct` verdicts for the rest). The
first failing cluster stops the drain (ledger kind `kg_duplicate_cluster`,
keyed by the sha1 of the cluster's node-id set, so a cluster that gains or
loses a node is a new item); set-aside clusters are skipped.

### Manager surfaces

| Manager | Role | Tools | Caller |
|---|---|---|---|
| **`kg_investigation_manager`** | read-only investigator | `kg_query` | `kg_finding_backlog_drain`, `kg_state_date_drain`, `/kg-maintenance/api/finding/<id>/investigate` |
| **`kg_resolution_manager`** | executor with full mutator suite | `kg_query` + 9 mutator tools + 2 finding-lifecycle ops | `kg_finding_executor_drain`, dev-page Accept button |
| `kg_mutation_manager` *(legacy — `/apply` route only)* | applies a structured `proposed_action` from older investigator reports | typed mutator allowlist | `/api/finding/<id>/apply` (legacy dev-page Apply button) |
| `kg_dev_manager` | free-form admin console | full mutator suite | `kg_dev_room` chat |
| `kg_dev_room_manager` | dispatch front for `kg_dev_manager` | `kg_dev_manager` (as a tool) | `kg_dev_room` chat |
| `kg_explorer_manager` | multi-step exploration with provenance | exploration tools | `emi_team_manager` delegation |

`kg_mutation_manager` is **not retired** — it still backs the legacy `/api/finding/<id>/apply` route. `kg_resolution_manager` is the canonical executor for autonomous runs (cron drain) and the dev-page Accept button. Plan: collapse `apply_one` into the same `execute_one` path so there's one executor, one audit trail, one toolkit; deferred until the legacy /apply path traffic is empirically zero.

### Skills + resources

| Artifact | Path | Used by |
|---|---|---|
| `kg-conflict-triage` skill | `skills/kg-conflict-triage/SKILL.md` | Both `kg_investigation::planner` and `kg_resolution::planner`. Combined doctrine + playbook (three-bucket triage, mutation gates, data-quality red flags, provenance walk, stop-early). Auto-injects on contradiction-flavored task keywords for any agent with `accept_auto_skills: true`. |
| `resource_kg_node_reading` | `resources/instructions/resource_kg_node_reading.md` | 10 agents that read KG node prose; teaches the present-tense canonical contract |
| `resource_kg_principles`, `resource_wiki_principles`, `resource_entity_card_principles` | `resources/instructions/` | **Orphaned** after the conflict-triage skill consolidation. Kept on disk for reference. |

### Wiki maintenance

| Component | Type | Purpose |
|---|---|---|
| `wiki_nightly_refresh` (cron 03:00) | function | Regenerate pages whose bullet text would render differently from sidecar |
| `wiki_growth` (cron 03:30) | function | Mint new pages for high-degree entities (importance + degree gates) |
| `page_writer` agent | LLM | Render page section-by-section from KG neighborhood |
| `wiki_consistency_critic` agent | LLM | Read fresh page + KG, flag contradictions → `wiki_contradiction` findings |
| `wiki_connection_investigator` agent | LLM | Read wiki prose, propose new KG edges |
| `wiki_renderer` module | code | Render markdown from KG neighborhoods (no LLM) |

### Entity card refresh (single nightly loop)

The earlier "two card pipelines" design (an `entity_cards` generate
pipeline + an `entity_card_maintenance_pipeline` SQL-scan auditor
writing to an `entity_card_maintenance_finding` table) was **removed**.
There is now one routine, `entity_card_refresh` (function, cron 04:30,
`app/assistant/pipelines/entity_cards_v2/refresh_subscriber.py`), and
card QA is a single L0 critic veto inside it — not a separate scan loop.

Each run, against `entity_card_v2`:
- **Rebuild stale cards** — a card is stale iff its entity node *or any
  current neighbor node* has `updated_at` newer than the card's
  `last_built_at`. Damped by a per-card cooldown (default 7 days — changes
  accumulate, one rebuild fires when the cooldown expires) and a per-night
  cap (default 10, highest node `importance` first).
- **Build new cards** for newly card-worthy entities (cap default 5). Each
  is built **inactive** and judged by `entity_cards::card_critic`
  (`gpt-5-mini`, mini tier; `app/assistant/agents/entity_cards/card_critic/`)
  — a 3-verdict L0 quality judge: `pass` / `rewrite` activate the card, a
  **veto** leaves it inactive so noise cards never go live. A failed critic
  call deletes the unjudged card, so the next run rebuilds and judges it.
- **Deactivate orphans** — cards whose entity node no longer exists (merge
  losers) are flipped `is_active=0`.

The first failed build or critic call stops the run (ledger kind
`entity_card`; set-aside entities are skipped). See `12_ENTITY_CARDS.md`.

There is no investigator+executor loop and no card-findings table on the
card side — staleness is stateless (the `updated_at` comparison), and the
only LLM gate is the critic veto on new builds.

## Dev page (`/kg-maintenance/investigated`)

Route: `app/routes/kg_maintenance.py`. Template: `app/templates/kg_maintenance_investigated.html`.

Each card shows:
- Disposition badge (`auto_apply` / `needs_user_review`)
- 24h grace timer (countdown for `auto_apply`)
- Recommendation textarea (autosaves on blur to `/api/finding/<id>/recommendation`)
- Operator notes textarea (autosaves to `/api/finding/<id>/operator_notes`)
- Diagnosis + evidence + open questions (read-only)
- Accept / Decline buttons:
  - **Accept**: calls `finding_executor.execute_one` immediately, bypassing the 24h gate
  - **Decline**: flips status to `dismissed` with the operator notes as `execution_notes`

The autosave endpoints are POSTs that take a single field; the view re-reads and re-renders on the next page load. There's no real-time websocket — refresh to see other reviewers' edits.

## Outstanding issues / known gaps

- **Synthetic_fact_review promotion path** — when the investigator
  certifies a `synthetic_fact_proposal` is genuinely new (not a
  duplicate), the promotion path (`approved → extracted → promoted`)
  is currently TBD. Investigator emits `disposition='needs_user_review'`
  for these and the user manually promotes via `/kg-dev`.
- **No concurrent-execution lock on `execute_one`** — two simultaneous
  execute_one calls on the same finding (cron + dev-page Accept, or
  two crons) will both invoke the LLM. In practice the second run's
  planner sees the post-mutation state via `kg_query` and produces a
  no-op (escalated) rather than corrupting data, but tokens are
  wasted. Fix: atomic status transition `investigated → executing`
  guarded by `WHERE status='investigated'` + 0-rowcount short-circuit.
- **Two competing TypeDecorators on `DateTime`** — `app/assistant/database/db_handler.py`
  defines a strict `UTCDateTime` (raises on naive writes) used by
  `UnifiedLog2026.timestamp`; `time_utils.py` defines the lenient
  `AwareUtcDateTime` (silent coerce) used everywhere else. They
  coexist by accident. Pick one and consolidate.
- **`server_default=func.now()` mixed-tz writes** — some columns
  declared `AwareUtcDateTime` still use `server_default=func.now()`
  which on SQLite returns a naive ISO string and bypasses the bind
  path. `kg_node_verdict.created_at` was already migrated to
  `default=utc_now`; sweep the remaining columns (claim_proposal,
  Ticket, kg_maintenance_finding) the same way.
- **Inverted date intervals** — some legacy `kg_node_metadata` rows
  have `start_date > end_date` from a pre-2026 migration. Audit:
  `SELECT id, label, start_date, end_date FROM kg_node_metadata
  WHERE start_date > end_date;` — fixed individually as discovered.
- **`wiki_consistency_critic` false positives** — the present-tense
  canonical trap (closed-era States rendered with present-tense
  prose look like contradictions). The `kg-conflict-triage` skill
  calls this out; the investigator dismisses these as
  `verdict_type='false_positive'`.
- **legacy `/api/finding/<id>/apply` route** — still wired through
  `kg_mutation_manager` with the old `proposed_action` schema. Used
  by an older dev-page button. Unify into `execute_one` when the
  legacy route's traffic empirically hits zero.

## How to find the producer of a finding

| Finding type | Producer | Findings table |
|---|---|---|
| `state_auto_closed` | `step_state_decay` (cron 02:45 — writes as `executed`) | `kg_maintenance_finding` |
| `state_missing_dates` | `step_missing_dates_scan` (weekly pipeline) | `kg_maintenance_finding` |
| `duplicate_node` | `step_duplicate_scan` (weekly pipeline; consults `kg_node_verdict`) | `kg_maintenance_finding` |
| `orphan_node` | `step_orphan_scan` (weekly pipeline) | `kg_maintenance_finding` |
| `wiki_contradiction` | `wiki_consistency_critic` (during `wiki_nightly_refresh`) | `kg_maintenance_finding` |
| `synthetic_fact_proposal` | `wiki_connection_investigator` (during `kg_wiki_inference`) | `kg_maintenance_finding` |
| Cluster lead with `cluster.root_question` | `kg_finding_cluster_resolver` | `kg_maintenance_finding` |

## Out of scope (mentioned for orientation)

- **`belief_engine`** — separate domain (beliefs vs KG nodes). Adjacent surface but its own pipelines + tables. See `docs/architecture/16_BELIEF_ENGINE.md`.
- **DB tables vs feature flags vs pipeline IDs sharing the name "entity_cards":** the DB table `entity_cards`, the user-settings feature flag `"entity_cards"`, and the pipeline_id `"entity_cards"` are three different namespaces that happen to share a string.

## Cookbook: where things live

| Concern | Path |
|---|---|
| Findings table model | `app/assistant/database/kg_maintenance_finding.py` |
| Verdicts table model | `app/assistant/database/kg_node_verdict.py` |
| Audit log model | `app/assistant/database/kg_revision_log.py` |
| Findings store + lifecycle helpers | `app/assistant/kg_maintenance/store.py` |
| Verdict store + readers | `app/assistant/kg_maintenance/verdict_store.py` |
| Investigator manager | `app/assistant/multi_agents/kg_investigation_manager/config.yaml` |
| Investigator agents | `app/assistant/agents/kg_investigation/{planner,final_answer}/` |
| Investigator brief builder | `app/assistant/kg_investigator/finding_brief.py` |
| Investigation driver | `app/assistant/kg_investigator/finding_processor.py` |
| Executor manager | `app/assistant/multi_agents/kg_resolution_manager/config.yaml` |
| Executor agents | `app/assistant/agents/kg_resolution/{planner,final_answer}/` |
| Execution driver | `app/assistant/kg_investigator/finding_executor.py` |
| Read-only query tool | `app/assistant/lib/core_tools/kg_query/kg_query_tool.py` |
| Mutator tool core | `app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py` |
| `kg-conflict-triage` skill | `skills/kg-conflict-triage/SKILL.md` |
| Dev-page route | `app/routes/kg_maintenance.py` |
| Dev-page template | `app/templates/kg_maintenance_investigated.html` |

For mutator-tool mechanics (FK-cascade trap, revision-log schema, the twelve-handler inventory, cookbooks for adding new ops or producers) see `docs/architecture/13_KG_MUTATOR_TOOLS.md`.
