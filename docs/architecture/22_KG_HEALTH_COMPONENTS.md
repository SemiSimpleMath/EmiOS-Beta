# KG Health Components — full inventory

Canonical reference of every component involved in maintaining the
knowledge graph + its derivatives (wiki pages, entity cards). When a
new finding shows up, this is the doc you want open.

This is the **canonical** description of the self-healing loop as of
2026-05-07. The older doc `13_KG_INVESTIGATION_MUTATION.md` predates
the take_action / verdict redesign and the `kg_resolution_manager`
consolidation; treat it as a historical artifact for tool-mechanics
reference only (FK-cascade trap, revision log schema, cookbooks).

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

```
DAILY (every night):
00:20  entity_cards (pipeline)              ← GENERATE cards (PageRank → write → prune)
02:00  kg_pipeline (pipeline)               ← INGEST: chat → claim_proposals
02:00  entity_card_maintenance_pipeline     ← AUDIT cards (6 SQL scans) — flipped to daily 2026-05-07
02:30  proposal_promoter (function)         ← MERGE BOT: promote claim_proposals to live KG nodes/edges
02:45  kg_state_decay (function)            ← auto-close stale States (writes findings as 'executed' — skips investigator)
02:50  kg_goal_dormancy_sweep (function)    ← active → dormant
02:55  kg_goal_outcome_detect (function)    ← "I finished X" → terminal close
03:00  kg_finding_backlog_drain (function)  ← INVESTIGATE: 5/day FIFO drain through investigator
03:00  wiki_nightly_refresh (function)      ← regen wiki pages whose bullets changed
03:15  kg_state_date_drain (function)       ← INVESTIGATE state_missing_dates findings (8/day, specialized brief)
03:30  kg_finding_cluster_resolve (function)← group N redundant findings into 1 lead
03:30  wiki_growth (function)               ← write new wiki pages for high-degree entities
03:45  kg_finding_executor_drain (function) ← EXECUTE: 10/day; picks auto_apply findings whose 24h grace expired
04:15  kg_wiki_inference (function)         ← propose new edges from wiki page prose

WEEKLY:
Mon 02:00  kg_maintenance_pipeline (pipeline)  ← heavy audit: orphan/embedding/dup/pagerank/desc/missing-dates

EVERY 30 MIN:
       kg_importance_rater (function)        ← node + edge importance scores (60n / 50e per tick)
```

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

**Planner agent** (`kg_investigation::planner`, `gpt-5.1` smart tier):
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

## Executor: `kg_resolution_manager`

Manager config: `app/assistant/multi_agents/kg_resolution_manager/config.yaml`. Same `MultiAgentManager` shell. Reads the investigator's prose recommendation and applies the corresponding mutations.

**Planner agent** (`kg_resolution::planner`, `gpt-5.5` smart tier):
- Statically binds the `kg-conflict-triage` skill (same doctrine as the investigator).
- `entity_card_level: 2` (entity context for mutation reasoning).
- Full mutator toolkit:

| Tool | Purpose |
|---|---|
| `kg_query` | Read-only SELECT/WITH (one statement, 200-row default cap) |
| `kg_close_state` | Set `end_date` on a State or Event |
| `kg_create_state_node` | Add a new era under an entity |
| `kg_update_node_field` | Surgical edit to one field (date, label, prose, etc.) |
| `kg_rename_label` | When the current label is demonstrably wrong |
| `kg_create_edge` / `kg_delete_edge` | Connecting/removing relationships |
| `kg_delete_node` | Remove orphans / fully-superseded nodes |
| `kg_merge_nodes` | Combine duplicates (label-spacing typos, alternate spellings) |

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
3. **`proposal_promoter` merge filter** — *planned*. Would drop `'distinct'`-verdict pairs at fact-extraction time, before the proposal_promoter merge logic fires. Higher leverage but more invasive; deferred for a follow-up commit.

## Component tables

### Producers (write findings)

| Producer | Type | finding_type | Notes |
|---|---|---|---|
| `step_orphan_scan` | maintenance pipeline step | `orphan_node` | Cheap structural scan |
| `step_duplicate_scan` | maintenance pipeline step | `duplicate_node` | Three-tier candidate gen + LLM confirm; consults `kg_node_verdict` to skip already-decided pairs |
| `step_description_gap_scan` | maintenance pipeline step | `missing_description` | Connected nodes only |
| `step_missing_dates_scan` | maintenance pipeline step | `state_missing_dates` | State/Event nodes missing one or both dates |
| `step_state_decay` | daily routine | `state_auto_closed` | Closes stale States, writes finding with `initial_status='executed'` so it skips the investigator |
| `wiki_consistency_critic` | wiki-pipeline agent | `wiki_contradiction` | Bypasses `upsert_finding` — writes rich `evidence_json` directly |
| `wiki_connection_investigator` | `kg_wiki_inference` routine | `synthetic_fact_proposal` | Investigator handles duplicate-check; promotion path is `synthetic_fact_review` |

### Cron functions (drains + audits)

| Routine | Function | Manager invoked |
|---|---|---|
| `kg_finding_backlog_drain` | `finding_processor.run_pending_findings` | `kg_investigation_manager` |
| `kg_state_date_drain` | `finding_processor.run_state_date_findings` | `kg_investigation_manager` (specialized brief) |
| `kg_finding_executor_drain` | `finding_executor.run_executable_findings` | `kg_resolution_manager` (only `auto_apply` findings past 24h grace) |
| `kg_finding_cluster_resolve` | `step_cluster_resolve.run` | `kg_finding_cluster_resolver` agent |
| `kg_state_decay` | `step_state_decay.run` | (none — pure SQL; writes `state_auto_closed` as `executed`) |
| `kg_goal_dormancy_sweep` | `step_goal_dormancy.run` | (none) |
| `kg_goal_outcome_detect` | `step_goal_outcome_detect.run` | `goal_outcome_detector` agent (one-shot) |
| `kg_wiki_inference` | `step_wiki_inference.run` | `wiki_connection_investigator` agent → claim_proposals |
| `kg_importance_rater` | rater function | (none) |

### Manager surfaces

| Manager | Role | Tools | Caller |
|---|---|---|---|
| **`kg_investigation_manager`** | read-only investigator | `kg_query` | `kg_finding_backlog_drain`, `kg_state_date_drain`, `/kg-maintenance/api/finding/<id>/investigate` |
| **`kg_resolution_manager`** | executor with full mutator suite | `kg_query` + 8 mutator tools | `kg_finding_executor_drain`, dev-page Accept button |
| `kg_mutation_manager` *(legacy — `/apply` route only)* | applies a structured `proposed_action` from older investigator reports | typed mutator allowlist | `/api/finding/<id>/apply` (legacy dev-page Apply button) |
| `kg_dev_manager` | free-form admin console | full mutator suite | `kg_dev_room` chat |
| `kg_dev_room_manager` | dispatch front for `kg_dev_manager` | `kg_dev_manager` (as a tool) | `kg_dev_room` chat |
| `kg_query_manager` | read-only sub-tool | `kg_query` | other managers via delegation |
| `kg_explorer_manager` | multi-step exploration with provenance | exploration tools | `emi_team_manager` delegation |
| `kg_team_manager` | bundle for `emi_team_manager` to delegate to | child managers | `emi_team_manager` |

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

### Two card pipelines (producer + auditor)

Despite confusing names, these are **distinct**:

| Component | Type | Purpose | LLM? | Findings table |
|---|---|---|---|---|
| **`entity_cards`** (cron 00:20) | pipeline (3 steps: PageRank → Generate → PruneDryRun) | **Generates** cards | yes | n/a |
| **`entity_card_maintenance_pipeline`** (cron 02:00 daily, was weekly) | pipeline (6 SQL-only scan steps) | **Audits** existing cards | no | `entity_card_maintenance_finding` |

Card maintenance was flipped from weekly Tuesday → daily 02:00 on
2026-05-07 so KG mutations get reflected in cards within ~24h instead
of up to 7 days. Long-term plan: cards regenerate on KG mutation
events, similar to how wiki dirty-detection works today.

Card-side findings (separate table, separate review route at
`/entity-card-maintenance`):

| Step | Output |
|---|---|
| `step_blank_content_scan` | `blank_card` (medium) |
| `step_broken_link_scan` | `broken_card_link` (high → deactivate) |
| `step_junk_name_scan` | `junk_card_name` (high → deactivate) |
| `step_low_confidence_scan` | `low_confidence_card` (medium) |
| `step_no_link_scan` | `unlinked_card` (low) |
| `step_stale_content_scan` | `stale_content` (low) |

There is no investigator+executor loop on the card side — just SQL scans + manual review.

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
- **`proposal_promoter` verdict filter** — planned (Reader #3 above).
  Would prevent re-merging at fact-extraction time, before findings
  are even raised.
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
| `missing_description` | `step_description_gap_scan` (weekly pipeline) | `kg_maintenance_finding` |
| `wiki_contradiction` | `wiki_consistency_critic` (during `wiki_nightly_refresh`) | `kg_maintenance_finding` |
| `synthetic_fact_proposal` | `wiki_connection_investigator` (during `kg_wiki_inference`) | `kg_maintenance_finding` |
| Cluster lead with `cluster.root_question` | `kg_finding_cluster_resolver` | `kg_maintenance_finding` |
| `blank_card`, `broken_card_link`, `junk_card_name`, `low_confidence_card`, `unlinked_card`, `stale_content` | `entity_card_maintenance_pipeline` steps (cron 02:00 daily) | `entity_card_maintenance_finding` |

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

For mutator-tool mechanics (FK-cascade trap, revision-log schema, six-handler contract, cookbooks for adding new ops or producers) see `docs/architecture/13_KG_MUTATOR_TOOLS.md`.
