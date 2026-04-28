# KG Investigation + Mutation — Self-Healing Loop

The knowledge graph cannot stay correct on extraction alone. Producers (the wiki critic, structural scans, decay jobs) write **findings** describing suspected problems; an **investigator** manager queries the live KG read-only and produces a structured diagnosis; a **mutator** manager picks up the diagnosis and either commits the suggested mutation through narrow typed tools or escalates to human review. Every mutation lands in `kg_revision_log` with before/after snapshots so it can be reverted. This is the self-healing loop — a closed pipeline from "something looks wrong" to "fixed (with audit trail)" or "queued for human."

> Cross-references: `02_MANAGERS.md` for the `MultiAgentManager` machinery the two managers extend; `09_KG_PIPELINE.md` for the upstream ingest path that creates the nodes these findings flag; `11_WIKI_GENERATOR.md` for the consistency critic, the most active finding producer.

## End-to-end flow

```
                      ┌──────────────────────────────┐
PRODUCERS             │ wiki_consistency_critic      │
(LLM critics +        │ duplicate_scan               │
 structural scans)    │ orphan_scan                  │
                      │ description_gap_scan         │
                      │ suspect_node_scan            │
                      │ state_decay                  │
                      └────────────┬─────────────────┘
                                   │ upsert_finding(...)
                                   ▼
                      ┌──────────────────────────────┐
                      │ kg_maintenance_finding       │
                      │   status='pending'           │
                      └────────────┬─────────────────┘
                                   │ kg_investigator.finding_processor
                                   │   .investigate_one(finding_id)
                                   ▼
                      ┌──────────────────────────────┐
                      │ kg_investigation_manager     │  read-only
                      │  planner ⇄ kg_query (+pods)  │  (scope_contract
                      │  → final_answer (structured) │   blocks all writes)
                      └────────────┬─────────────────┘
                                   │ persist investigation_report_json
                                   ▼
                      ┌──────────────────────────────┐
                      │ kg_maintenance_finding       │
                      │   status='investigated'      │
                      │   investigation_report_json  │
                      └────────────┬─────────────────┘
                                   │ kg_investigator.finding_executor
                                   │   .execute_one(finding_id)
                                   ▼
                      ┌──────────────────────────────┐
                      │ kg_mutation_manager          │  writes
                      │  planner → typed mutator     │  (write_kg=True
                      │  → kg_finding_resolve OR     │   granted on Message,
                      │    kg_finding_escalate       │   not by config)
                      └────────────┬─────────────────┘
                                   │ on every mutation:
                                   ▼
                      ┌──────────────────────────────┐
                      │ kg_revision_log              │
                      │   op, before_json,           │
                      │   after_json, finding_id     │
                      └──────────────────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
     status='executed'                         status='approved'
     (auto-applied; reversible)                (queued for /kg-maintenance
                                                human review)
```

The loop above is **Path A** — the new, LLM-driven, fully-automated route. It writes mutations through one tool (`KGMutatorTool`) and audits via `kg_revision_log`.

There is also a **Path B**, which predates Path A. Both paths are live today.

## Two execution paths (the duality)

```
                              FINDING (status='pending')
                                       │
                  ┌────────────────────┴────────────────────┐
                  ▼                                         ▼
        PATH A — automated                        PATH B — human-approved
        (LLM-driven, recent)                      (typed, legacy)
                  │                                         │
                  │                                         │
        kg_investigator                          /kg-maintenance UI
        .finding_processor                       human reviews + clicks
        invokes                                  Approve / Reject /
        kg_investigation_manager                 Execute
        (read-only)                              POST /api/action
                  │                                         │
                  │ writes report                           │ flips status
                  ▼                                         ▼
        status='investigated'                    status='approved'
        + investigation_report_json                        │
                  │                                         │
        kg_investigator                          kg_maintenance/store.py
        .finding_executor                        .execute_finding(id)
        invokes                                  delegates to
        kg_mutation_manager                      step_execute_findings
        (LLM planner picks op                    .execute_single_finding
         from proposed_action,                   (Python switch on
         applies via                              finding_type)
         kg_mutator_tool, writes
         kg_revision_log)                        ┌─ orphan_node:    direct SQL delete
                  │                              ├─ duplicate_node: direct SQL merge
                  ▼                              │                  + kg_merge_log
         status='executed'                       │
         OR                                      └─ everything else: no executor,
         status='approved' (escalated)              returns "no executor for type X"
         on irreversible / low confidence                  │
                                                            ▼
                                                  status='executed'
                                                  (no kg_revision_log row;
                                                   only kg_merge_log for
                                                   the duplicate_node case)
```

### What's actually different between the two

| | Path A (automated) | Path B (human-approved) |
|---|--------------------|-------------------------|
| **Triggered by** | investigator running on `pending` findings | human clicking Approve in `/kg-maintenance` |
| **Operates on** | `status='investigated'` (after a report exists) | `status='approved'` |
| **Dispatcher** | LLM planner (`kg_mutation::planner`) — reads `proposed_action.op` from the report and decides apply vs escalate | Python switch on `finding_type` in `step_execute_findings.execute_single_finding` |
| **Finding types covered** | Whatever the investigator's prompt + planner know how to handle (currently all the producer types) | **Only 2**: `orphan_node`, `duplicate_node`. Others return "no executor for finding_type X". |
| **Mutation mechanism** | `kg_mutator_tool` (the typed-tool layer with safety rails — refuses cross-type merges, refuses rename collisions, etc.) | **Direct SQL** — bypasses `kg_mutator_tool` entirely. Has its own `_pick_canonical`, `_llm_merge_fields`, `_node_to_dict` logic for duplicate_node. |
| **Audit table** | `kg_revision_log` (with before/after JSON snapshots) | `kg_merge_log` (only for duplicate_node merges) — orphan deletions get no audit at all |
| **Reversibility model** | Every commit captures before+after, can be reverted | Merges have `kg_merge_log`; deletions are gone |
| **Safety-rail location** | Inside `kg_mutator_tool` — applies to every caller of the tool | Inline in the legacy executor; no shared layer |

### Why the duality exists

Code archaeology, not design. Path B is the original "self-healing" mechanism: `/kg-maintenance` review UI plus a typed-switch executor was built before `kg_mutation_manager` existed. When the investigator + mutation manager landed (this past week), they were built as a separate parallel route rather than refactored to drive the legacy step. Both paths grew up at different times, both work, neither was forced to converge.

### Why this is a 🔴 (split-brain risk)

1. **Two writers, two safety layers, two audit tables.** Adding a "refuse merge if confidence < 0.9" rule has to be added in *both* `kg_mutator_tool` *and* `step_execute_findings` to apply uniformly. If you add it only in the new tool, Path B silently bypasses it.
2. **Path B's coverage is partial.** Of the 7+ finding types producers raise, only 2 are auto-executable through Path B. Approving a `wiki_contradiction` or `missing_description` from `/kg-maintenance` returns "no executor for finding_type X" — the human's approval is a no-op. (Today this is masked by the fact that those types tend to flow through Path A's investigator instead.)
3. **No shared notion of `before_json`.** Path B's `kg_merge_log` and Path A's `kg_revision_log` have different schemas. A future "show me everything that changed in the KG last week" query has to consult both tables.

### Eventual unification (sketch — deferred)

The right shape is one executor with two ways to populate its input:

```
Finding raised
  │
  ├─ no report yet ──► investigator writes report ──► auto-decide
  │
  └─ human at /kg-maintenance writes a "decision_brief"
     (op + optional clarification + optional constraints, e.g.
      "merge but don't promote alias") ──► same kg_mutation_manager
     picks it up, treats human input as the report, applies via
     kg_mutator_tool with full audit
```

This would: (a) collapse the two audit tables into one; (b) push the legacy step's special-case logic for duplicate_node into reusable typed mutator ops; (c) let humans add nuance ("merge them but don't carry over the alias") instead of binary approve/reject. Not in scope today — listed for the inevitable cleanup pass.

> **For contributors today**: when adding a new finding_type or mutation behavior, prefer Path A. Put safety rules in `kg_mutator_tool`, not the dispatchers. If you must add to Path B (the legacy step), at least log the rule duplication in a `# DUPLICATE-WITH:` comment so it surfaces at unification time.

## `kg_maintenance_finding` table

`app/assistant/database/kg_maintenance_finding.py:33`. One row per suspected problem regardless of finding type — flexible subject columns (`primary_node_id` always set; `secondary_node_id` for pairs; `edge_id` for edge-level findings).

Lifecycle columns:

| Column | Role |
|---|---|
| `finding_type` | What the producer found |
| `status` | Where the row sits in the loop |
| `priority` | `high` / `medium` / `low`; sorts the review queue |
| `suggested_action` | Producer's first guess; the investigator can override |
| `reason`, `confidence` | Producer's natural-language summary + 0–1 confidence |
| `evidence_json` | Raw context the producer saw (so the UI doesn't re-query) |
| `investigation_report_json` | Structured report from `kg_investigation_manager` |
| `investigated_at` | Set when status flips to `investigated` |
| `executed_by`, `executed_at`, `execution_notes` | Filled by mutator or UI |
| `pipeline_run_id` | Lets the producing pipeline batch-investigate just its own finds |

Indices include `(finding_type, primary_node_id, secondary_node_id, status)` — see `__table_args__` (`kg_maintenance_finding.py:88`) — supporting the dedup query in `upsert_finding`.

### `finding_type`

| Value | Producer | Meaning |
|---|---|---|
| `duplicate_node` | `step_duplicate_scan` | Two nodes that should be merged |
| `suspect_node` | `step_suspect_node_scan` (currently disabled in pipeline) | Low-quality node — delete or fix |
| `orphan_node` | `step_orphan_scan` | Node has zero edges |
| `duplicate_edge` | (planned) | Multiple edges between same `(src, tgt, type)` |
| `missing_description` | `step_description_gap_scan` | Node lacks `description` |
| `type_error` | (planned) | `node_type` is wrong / malformed |
| `wiki_contradiction` | `wiki_consistency_critic` | Wiki prose contradicts KG / source |
| `state_auto_closed` | `step_state_decay` | Decay job closed a stale State; surfaced for confirmation |

> Note: `state_auto_closed` is recorded in the table but is not in the docstring's enum list (`kg_maintenance_finding.py:9`). It is emitted by `step_state_decay.py:142` after the closure has already happened — the finding is informational rather than a request for action.

### `status`

`pending → investigated → executed | approved | rejected`. Plus `execute_error` set by the legacy `step_execute_findings` path when an auto-execute fails.

- `pending`: producer wrote it; no one has looked.
- `investigated`: `kg_investigation_manager` ran; `investigation_report_json` is populated.
- `executed`: a mutation was applied (auto via `kg_mutation_manager`, or manually via `/kg-maintenance` UI).
- `approved`: queued for human action — set by `kg_finding_escalate` from the mutator, OR by the user clicking "Flag selected for action" in the UI.
- `rejected`: dismissed by a human.

### `suggested_action`

Free-form per producer: `merge`, `delete`, `retype`, `add_description`, `review`. The investigator's `proposed_action.op` is the load-bearing field downstream — `suggested_action` is informational.

## `kg_revision_log` table

`app/assistant/database/kg_revision_log.py:33`. One row per attempted mutation. Reversibility lives here: each row carries enough state in `before_json` / `after_json` to undo the change.

| Column | Role |
|---|---|
| `op` | Mutation kind (see vocab below) |
| `args_json` | The arguments the agent supplied to the typed tool |
| `before_json` | Snapshot(s) of affected rows pre-mutation |
| `after_json` | Snapshot(s) post-mutation (or `"DELETED"` for deletions) |
| `reason` | Mandatory free-text justification — the audit trail |
| `finding_id` | Source finding row (when the mutation came from one) |
| `agent_id` | Free-form actor (`kg_finding_executor`, `ui::merge_form`, …) |
| `succeeded` | `1` on commit, `0` on attempted-but-failed (for retry / audit) |
| `error_message` | Error text when `succeeded=0` |
| `reverted_at`, `reverted_by` | Set when the change has been undone |

### `op` vocabulary

| op | Source | Reverses to |
|---|---|---|
| `merge_nodes` | `kg_merge_nodes` tool | re-create the folded node, restore original `source_id`/`target_id` on the rewritten edges |
| `rename_label` | `kg_rename_label` tool | restore old label; aliases preserve it as fallback |
| `update_node_field` | `kg_update_node_field` tool | restore the single before-value |
| `delete_edge` | `kg_delete_edge` tool | re-insert the snapshotted edge row |
| `finding_resolve` | `kg_finding_resolve` tool | flip the finding back to `investigated` |
| `finding_escalate` | `kg_finding_escalate` tool | flip the finding back to `investigated` |

Reversal logic itself is not yet implemented — the snapshots make it possible, but no `revert_revision()` function exists today. The redundancy of `before_json` is what gives this loop its safety: even an irreversible-in-spirit op like merge can be reconstructed from the log.

## Producers

| Producer | File | finding_type | Notes |
|---|---|---|---|
| `wiki_consistency_critic` | `app/assistant/wiki_generator/consistency_critic.py:241` | `wiki_contradiction` | Critic on each wiki page; bypasses `upsert_finding` — writes the row directly with rich `evidence_json` (quoted text, paragraph, line number, source statement) |
| `duplicate_scan` | `app/assistant/pipelines/kg_maintenance_pipeline/step_duplicate_scan.py:434` | `duplicate_node` | Three-tier candidate gen + LLM confirm. Emits per pair, not per group — splits a 3-node merge group into two pair findings |
| `orphan_scan` | `app/assistant/pipelines/kg_maintenance_pipeline/step_orphan_scan.py:43` | `orphan_node` | Cheap structural scan; `priority=low` |
| `description_gap_scan` | `app/assistant/pipelines/kg_maintenance_pipeline/step_description_gap_scan.py:44` | `missing_description` | Connected nodes only (orphans get their own finding) |
| `suspect_node_scan` | `app/assistant/pipelines/kg_maintenance_pipeline/step_suspect_node_scan.py:193` | `suspect_node` | Per-node LLM quality check; not currently wired into the pipeline (`pipeline.py:15`) |
| `state_decay` | `app/assistant/pipelines/kg_maintenance_pipeline/step_state_decay.py:141` | `state_auto_closed` | Auto-closes stale States, emits an informational finding for confirmation |
| Entity-card scans | `app/assistant/pipelines/entity_card_maintenance_pipeline/step_*.py` | `stale_content`, `no_kg_link`, `low_confidence`, `junk_name`, `broken_kg_link`, `blank_content` | Card-level findings — `entity_card_id` is set on a different store/table; not consumed by the KG investigator/mutator path |

All KG-side producers route through `kg_maintenance.store.upsert_finding` (`store.py:36`) which:
- Normalises `(primary, secondary)` pair ordering so `(A, B)` and `(B, A)` collapse to one dedup key.
- Skips if a `pending` or `approved` row already exists for the same `(finding_type, primary, secondary)` triple. `rejected` and `executed` rows do **not** block re-raising — if the issue resurfaces, the scan picks it up again.
- Returns `(finding_id, created)` so the producer can count fresh finds.

`KGMaintenancePipeline.run` calls `_investigate_findings_for_run(run_id)` as its final step (`pipeline.py:106`) — the producer sweep automatically queues an investigation pass scoped to the same `pipeline_run_id`, capped at 20 investigations per run.

## Investigator: `kg_investigation_manager`

Manager config: `app/assistant/multi_agents/kg_investigation_manager/config.yaml`. Standard `MultiAgentManager` with the emi_team delegator + summary pattern, planner agent (`kg_investigation::planner`), and structured `final_answer` agent.

### Read-only enforcement

Two layers:

1. `tools.allowed_tools` and `scope_contract.tools.allowed_tools` both list only `kg_query`, `pod_search`, `pod_fetch`, `ask_user` (`config.yaml:60` and `:67`). The scope_contract's `blocked_tools` enumerates every KG mutation tool by name (`config.yaml:74`).
2. `scope_contract.writes.write_kg: false` (`config.yaml:84`) — even if a tool slipped through, the scope context refuses to authorize a KG write.

### Tools

- `kg_query` (`app/assistant/lib/core_tools/kg_query/kg_query_tool.py`) — single read-only SELECT (or `WITH ... SELECT`) per call. Connection opens with `mode=ro` (line 113), `PRAGMA query_only=ON` (line 122). Statement parser rejects anything not starting with `select` or `with`, plus a small PRAGMA allowlist (`table_info`, `index_list`, `index_info`, `foreign_key_list`, `table_list` — line 43). Default 200-row cap, absolute 5000-row max, 5s timeout. One statement only — no semicolon-chained statements (line 76).
- `pod_search` / `pod_fetch` — addressable chat clusters. The investigator reaches for these when KG alone is too narrow (e.g. recent conversation context around a flagged entity).
- `ask_user` — for clarifying the task only, not for asking the user to interpret data.

### Brief construction

`app/assistant/kg_investigator/finding_brief.py` turns one finding row into a `(task, information)` pair the manager consumes as a normal `Message`. Branches per `finding_type`:

- `wiki_contradiction` → `_brief_wiki_contradiction` includes the quoted prose, surrounding paragraph, section heading, line number, the disputed source statement, the subject node block, and its 30-edge neighborhood.
- `duplicate_node` / `duplicate_edge` → `_brief_node_pair` includes both nodes side-by-side with neighborhoods.
- `suspect_node` / `orphan_node` / `missing_description` / `type_error` → `_brief_single_node` includes the subject + neighborhood + the producer's `evidence_json`.

The `task_phrase` per branch tells the planner exactly what kind of `proposed_action` to produce. The brief stays as text — not structured fields — so the planner reads it like any other task.

### Planner

`app/assistant/agents/kg_investigation/planner/` — `gpt-5.1` smart tier. The system prompt (`prompts/system.j2`) is the schema cheatsheet + investigation discipline guide. Key disciplines:

- **One question per query** — walk the investigation, don't try to compute everything in a single 8-JOIN.
- **Counts before bodies** — `COUNT(*)` first, then sample rows.
- **Don't speculate beyond the data** — if the data can't answer, say so and surface what would.
- **Don't try to mutate** — the planner is told the manager is read-only by scope; even if the task says "fix X", recommend the action, don't try to perform it.

The prompt includes a date-semantics primer (`prompts/system.j2:90`) describing the present-tense canonical contract for State / Event / Goal nodes — bug class taxonomy for wiki contradictions.

The planner action vocabulary is `kg_query | pod_search | pod_fetch | ask_user | return_control` (`agent_form.py`).

### Final answer (the report)

`app/assistant/agents/kg_investigation/final_answer/agent_form.py` defines the report schema:

```python
class ProposedAction(BaseModel):
    op: str            # merge_nodes | split_node | delete_edge |
                       # update_node_field | rename_label | no_action | escalate_user
    args: str          # free-form description of args
    reversibility: str # reversible | partially_reversible | irreversible
    confidence: float  # 0.0 to 1.0

class AgentForm(BaseModel):
    diagnosis: str
    evidence: List[EvidenceItem]   # (query, finding) pairs grounding the diagnosis
    proposed_action: Optional[ProposedAction]
    open_questions: List[str]
    final_answer_answer: str       # markdown rendering of the structured fields
    result_summary: str
```

The `proposed_action` is the contract handed to the mutator — the structured fields are the source of truth, the markdown is a faithful rendering for human consumption.

### Persistence

`finding_processor.py:74` extracts the report from the `kg_investigation::final_answer` audit message (matching by sender suffix) and writes it back to `KGMaintenanceFinding.investigation_report_json`, sets `investigated_at`, flips status to `investigated`. `investigate_one` is the unit operation; `investigate_findings(ids, ...)` (called by the pipeline final step) and `run_pending_findings(limit, types=...)` (for routine wiring) are bounded sweeps over it.

## Mutator: `kg_mutation_manager`

Manager config: `app/assistant/multi_agents/kg_mutation_manager/config.yaml`. Same `MultiAgentManager` shell, smaller tool surface, smaller cycle cap (`max_cycles: 16` vs the investigator's `40`). Planner is `gpt-5.4-mini` — the decision is rule-based, not investigative.

### Tools

- The five typed mutator/finding tools: `kg_merge_nodes`, `kg_rename_label`, `kg_update_node_field`, `kg_finding_resolve`, `kg_finding_escalate`.
- `kg_query` for one-shot ID lookups when the proposed action's args reference a label rather than a node id.
- `ask_user` (rarely used here).

`scope_contract.writes.write_kg: true` (`config.yaml:93`) is what authorizes the mutation tools to actually fire.

### The scope-context trick

The system refuses to widen `writes.write_kg` from `False` to `True` via `scope_contract` — that path can only narrow, never grant. So `finding_executor.py:35` builds a permissive scope and stamps it on the inbound `Message`:

```python
def _mutation_scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::kg_investigator::finding_executor",
        owner_id="primary_user",
        actor_id="kg_finding_executor",
        surface="system",
        room_id=None,
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(write_kg=True, write_unified_log=True),
    )
```

The manager's own `scope_contract` then narrows the tools to the typed mutator allowlist. Net effect: the manager can write to the KG, but only via the six typed tools — it cannot reach for `kg_create_node`, `kg_delete_node`, etc., even though it has the writer privilege.

### Planner decision rules

`app/assistant/agents/kg_mutation/planner/prompts/system.j2:14` — applied in order:

1. `op == "no_action"` → call `kg_finding_resolve(finding_id, action="no_action", reason=…)` and `return_control`. No mutation.
2. `op == "escalate_user"` → call `kg_finding_escalate(finding_id, summary=…, suggested_action=…, reason=…)` with the diagnosis as the summary. `return_control`.
3. `reversibility == "irreversible"` OR `confidence < 0.75` → too risky / too uncertain to auto-apply. Escalate.
4. Otherwise → execute the matching typed tool, then **immediately** call `kg_finding_resolve(finding_id, action=<op>, notes=<revision_log_id>, reason=…)`. `return_control`.

Failure handling: if a mutation tool returns `ok: false`, the planner does not retry — it escalates with the error in the summary so a human can intervene.

The planner is explicitly told **not** to second-guess the investigator. The proposed action is the contract; if the investigator was wrong, escalate, don't decide for them. `kg_query` is permitted only for ID lookups when args are ambiguous (one small lookup, then act). `dry_run: true` is encouraged on the first attempt when args were inferred.

### Final answer (the outcome)

`app/assistant/agents/kg_mutation/final_answer/agent_form.py`:

```python
class AgentForm(BaseModel):
    outcome: str             # applied | escalated | no_action | error
    op_applied: Optional[str]
    revision_log_id: Optional[str]
    finding_status: Optional[str]   # 'executed' or 'approved'
    error_message: Optional[str]
    final_answer_answer: str
    result_summary: str
```

The final-answer prompt instructs the agent to **read recent_history**, not infer from the inbound task — the planner's actual tool calls are the source of truth for what happened.

### Driver

`app/assistant/kg_investigator/finding_executor.py`:
- `_claim_executable_finding_ids(limit)` picks the oldest rows where `status='investigated' AND investigation_report_json IS NOT NULL`.
- `_build_brief(finding_id)` constructs `(task, information)` from the report's `proposed_action` plus the finding context (ids, types, priority).
- `execute_one(finding_id)` invokes the manager with the permissive scope, then `_extract_outcome_from_audit` pulls the structured outcome from the final-answer message.
- `run_executable_findings(limit=N)` is the bounded sweep; it tallies `applied / escalated / no_action / errors`.

The loop is idempotent because the manager itself updates status via `kg_finding_resolve` / `kg_finding_escalate` — a second sweep skips the same rows.

## The six typed mutator ops

All six are dispatched by tool name through one `KGMutatorTool.execute` (`app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py:159`) → `handle_<tool_name>`. The tool wrappers (`app/assistant/lib/tools/kg_*/`) are one-line loaders that point back at `KGMutatorTool` — there's a single class with six handlers, not six classes.

| op | Required args | Side effect | Reversibility | Safety rails |
|---|---|---|---|---|
| `kg_merge_nodes` | `keep_id`, `fold_id`, `reason` | Rewrite all `fold_id` edges to `keep_id`; union aliases; delete `fold_id` | partial — node identity gone, but `before_json` has full snapshot incl. all rewritten edges | Refuses `keep.node_type != fold.node_type` (`kg_mutator_tool.py:230`); refuses `keep_id == fold_id` |
| `kg_rename_label` | `node_id`, `new_label`, `reason` | Set new label; preserve old label as alias | reversible | Refuses if another active node of the same `node_type` already has `new_label` (`kg_mutator_tool.py:340`); error message tells the agent to use `kg_merge_nodes` instead |
| `kg_update_node_field` | `node_id`, `field`, `value`, `reason` | Single-field update | reversible | `field` must be in `_ALLOWED_UPDATE_FIELDS` allowlist (`kg_mutator_tool.py:47`) — narrow fields only (description, dates, importance, etc.); `aliases` / `hash_tags` get a `list_op` (`set` / `add` / `remove`) so partial updates don't clobber |
| `kg_delete_edge` | `edge_id`, `reason` | Single edge removed | reversible (snapshot in `before_json`) | None beyond reason requirement |
| `kg_finding_resolve` | `finding_id`, `action`, `reason` (+ optional `notes`) | Sets finding `status='executed'`, records `executed_by` from scope.actor_id | reversible | None |
| `kg_finding_escalate` | `finding_id`, `summary`, `suggested_action`, `reason` | Sets finding `status='approved'` (the existing vocab for "queued for human review") | reversible | None |

Common contract:
- Every call requires a non-empty `reason`. Empty `reason` raises `ValueError("reason is required for every mutation op.")` (`kg_mutator_tool.py:198`).
- `dry_run: true` returns the diff that would be applied without touching the KG — content is `"DRY RUN: would …"`, data carries `before` + `after` previews.
- Optional `finding_id` ties the revision back to its source finding; copied into both `args_json` and the dedicated `finding_id` column on `kg_revision_log`.
- Errors are surfaced as `ToolResult(data={"ok": False, ...})` rather than raised — so the calling agent can inspect and decide (per the planner's failure rule, escalate).

## The merge_nodes FK-cascade trap

`kg_mutator_tool.py:283`:

```python
for e in in_edges:
    e.target_id = keep_id
for e in out_edges:
    e.source_id = keep_id
keep.aliases = new_aliases
# Flush BEFORE delete so the FK CASCADE on Edge.{source,target}_id
# sees edges already pointing at keep, not still-pointing-at-fold.
# Without this, SQLite cascades fold's deletion and nulls out the
# rewritten edges' source_id/target_id, hitting NOT NULL.
session.flush()
session.delete(fold)
```

Without the explicit `session.flush()`, SQLAlchemy's unit-of-work batched the rewrites and the delete together. SQLite saw `delete fold` before it saw the `update edge.target_id = keep`, the FK cascade fired on the still-fold-pointing edges, and the rewritten edges came out with NULL endpoint columns — hitting the `NOT NULL` constraint and aborting the transaction.

This bites every mutator that combines "rewrite FK references" with "delete the old parent row." If you're adding a new mutator op (`split_node`, `delete_node`, edge target rewrites, …), `flush()` between the rewrite and the delete is load-bearing. Don't trust the implicit ordering.

## Human review queue (`/kg-maintenance`)

Route: `app/routes/kg_maintenance.py`. Template: `app/templates/kg_maintenance.html`.

Two pages:
- `GET /kg-maintenance/` — triage dashboard. Filterable by `status` / `type` / `priority`. Default view is `pending`. Each row shows the finding type badge, priority, primary/secondary node blocks (label + node_type + edge count), suggested action, reason, confidence. Reviewers select rows and click **"Flag selected for action"** (→ `approved`) or **"Reject selected"** (→ `rejected`).
- `GET /kg-maintenance/queue` — the action queue. Just the `approved` rows, split into auto-executable (`duplicate_node`, `orphan_node`) vs. needs-human-review.

API endpoints:
- `POST /api/action` with `{id, action: approve|reject|execute}` — single-row state change. `execute` runs `store.execute_finding(id)` which delegates to the legacy `step_execute_findings.execute_single_finding` (a separate path from `kg_mutation_manager` — the UI's executor predates the LLM mutator and still uses direct typed dispatch).
- `POST /api/bulk_action` with `{ids: [...], action}` — bulk approve / reject.
- `POST /api/execute_approved` — runs every `approved` row through `execute_single_finding`, sets `executed` or `rejected` based on outcome.
- `POST /api/run` — triggers a `KGMaintenancePipeline` scan; never auto-executes (`execute_findings` is forced into `skip_steps`).

Status transitions visible to the user:

```
pending ──UI: approve──→ approved ──UI: execute / batch──→ executed
        │                          
        └──UI: reject───→ rejected
                          
investigated ──mutator: kg_finding_resolve──→ executed
             ──mutator: kg_finding_escalate──→ approved (queued for UI)
```

> See [Two execution paths (the duality)](#two-execution-paths-the-duality) earlier in this doc for the full comparison. Briefly: Path A (LLM-driven) operates on `status='investigated'`; Path B (legacy, this UI's executor) operates on `status='approved'`. They use **different** mutation mechanisms, **different** audit tables, and Path B only covers `orphan_node` + `duplicate_node`.

## End-to-end test reference

`_test_pieper_merge.py` at the repo root is the canonical end-to-end exercise. It seeds a `duplicate_node` finding for the Dane / Dana alias-typo case, runs `execute_one(finding_id)`, and verifies the merge committed and the finding flipped to `executed`. The script also resets the finding state between runs (the FK-cascade bug had previously bumped it to `approved` via escalation) — useful as a template for any mutator regression test.

## Key files

| Concern | Path |
|---|---|
| Findings table model | `app/assistant/database/kg_maintenance_finding.py` |
| Audit log model | `app/assistant/database/kg_revision_log.py` |
| Investigator manager | `app/assistant/multi_agents/kg_investigation_manager/config.yaml` |
| Investigator agents | `app/assistant/agents/kg_investigation/{planner,final_answer}/` |
| Mutator manager | `app/assistant/multi_agents/kg_mutation_manager/config.yaml` |
| Mutator agents | `app/assistant/agents/kg_mutation/{planner,final_answer}/` |
| Brief builder | `app/assistant/kg_investigator/finding_brief.py` |
| Investigation driver | `app/assistant/kg_investigator/finding_processor.py` |
| Mutation driver | `app/assistant/kg_investigator/finding_executor.py` |
| Read-only query tool | `app/assistant/lib/core_tools/kg_query/kg_query_tool.py` |
| Typed mutator core (six handlers) | `app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py` |
| Mutator tool wrappers | `app/assistant/lib/tools/kg_{merge_nodes,rename_label,update_node_field,finding_resolve,finding_escalate}/` |
| Findings store (CRUD + dedup) | `app/assistant/kg_maintenance/store.py` |
| Producer pipeline | `app/assistant/pipelines/kg_maintenance_pipeline/pipeline.py` |
| Wiki critic producer | `app/assistant/wiki_generator/consistency_critic.py` |
| Review UI route | `app/routes/kg_maintenance.py` |
| Review UI template | `app/templates/kg_maintenance.html` |
| End-to-end test | `_test_pieper_merge.py` |

## Cookbook: adding a new producer

1. Pick a `finding_type` string. Reuse an existing one if your producer detects the same class of problem; coin a new one if not.
2. In your scan, call `app.assistant.kg_maintenance.store.upsert_finding(...)` with `finding_type`, `primary_node_id`, `suggested_action`, plus optional `secondary_node_id` / `edge_id` / `reason` / `confidence` / `priority` / `evidence_json` / `pipeline_run_id`. Dedup is automatic on `(finding_type, primary, secondary, status in {pending, approved})`.
3. Add a brief branch in `kg_investigator/finding_brief.py:build_finding_brief` for your `finding_type` — pre-fetch the obvious context (subject node, neighborhood, type-specific evidence) so the investigator's query budget goes to real digging.
4. Set the `task_phrase` to tell the planner what `proposed_action.op` you expect (one of the six mutator ops, or `escalate_user`, or `no_action`).
5. If your producer runs in a pipeline, call `_investigate_findings_for_run(run_id)` (or pass your fresh ids to `investigate_findings`) at the end so the investigation pass is bounded to your sweep.

## Cookbook: adding a new mutator op

1. Add `handle_<tool_name>` to `KGMutatorTool` (`app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py`). It must:
   - Validate args; raise `ValueError` for invalid input.
   - Open one `get_db_manager().transaction(op="kg_mutator.<op>")` session.
   - Snapshot affected rows into `before` *before* mutating.
   - If you delete a row whose children FK-reference it, **`session.flush()`** between the children-rewrite and the parent delete (see "FK-cascade trap" above).
   - Call `_write_revision_log(...)` with `op`, `args`, `before`, `after`, `reason`, `finding_id`, `agent_id` — every commit gets a row.
   - Return `ToolResult(data={"ok": True, "revision_log_id": rid, ...})`. On failure, `ok: False` with details.
   - Honour `dry_run: true` — return the would-apply diff without touching the DB.
2. Add a thin tool-wrapper directory under `app/assistant/lib/tools/<tool_name>/` with:
   - `__init__.py` containing the standard one-liner: `get_tool_class = create_tool_loader(KGMutatorTool)`.
   - `tool_contract.json` describing inputs/outputs/metadata (copy from `kg_merge_nodes/tool_contract.json` as a template).
   - `prompts/<tool_name>_description.j2` and `prompts/<tool_name>_args.j2`.
   - `tool_forms/tool_forms.py` for the Pydantic argument schema.
3. Add the new tool name to `kg_mutation_manager/config.yaml` under `tools.allowed_tools`, `scope_contract.tools.allowed_tools`, and `tool_visibility.always_show`.
4. Extend the `ProposedAction.op` enum description in `app/assistant/agents/kg_investigation/final_answer/agent_form.py` so the investigator knows it can recommend the new op.
5. Update the planner's mapping rules in `app/assistant/agents/kg_mutation/planner/prompts/system.j2:23` so the mutator routes the new op to the new tool.
