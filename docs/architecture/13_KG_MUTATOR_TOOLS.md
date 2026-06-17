# KG Mutator Tools — handlers, audit log, and trap notes

Reference for the typed mutator tools that actually write to
`kg_node_metadata` / `kg_edge_metadata`. For the broader self-healing
loop (investigator, executor, verdicts, dev page) see
`22_KG_HEALTH_COMPONENTS.md` — that doc owns the orchestration shape;
this doc owns the tool-level mechanics.

## One class, many handlers

All KG mutator tools dispatch through a single `KGMutatorTool.execute`
in `app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py`. Each
tool wrapper under `app/assistant/lib/tools/kg_*/` is a one-line loader
that points back at this class:

```python
get_tool_class = create_tool_loader(KGMutatorTool)
```

The router then calls `handle_<tool_name>` for the inbound tool. There
is **one class with N handlers**, not N classes — adding an op is one
new method plus a thin wrapper directory.

## Handler inventory

`KGMutatorTool` backs **twelve** tool names (the module docstring lists
them): ten KG-mutation ops plus two finding-lifecycle ops
(`kg_finding_resolve` / `kg_finding_escalate`). Destructive ops refuse
user-locked rows (`locked_by_user_at`, the axiom layer) — merge, rename,
update_field, close_state, delete_node, delete_edge, repoint_edge,
split_succession; additive `create_*` ops are exempt; `kg_set_user_lock`
is the ONE op allowed to touch a locked row (it's how the user revokes
the lock — see `_refuse_if_locked` and its absence from `handle_kg_set_user_lock`).

| Tool | Required args | Side effect | Reversibility | Safety rails |
|---|---|---|---|---|
| `kg_merge_nodes` | `keep_id`, `fold_id`, `reason` | Rewrite all `fold_id` edges to `keep_id`; union aliases; rebind dependent rows; delete `fold_id` | partial — node identity gone, but `before_json` carries full snapshot incl. all rewritten edges | Refuses `keep.node_type != fold.node_type`; refuses self-merge; refuses when `fold` outranks `keep` (pagerank / edge count) unless `force=true`; refuses locked rows |
| `kg_rename_label` | `node_id`, `new_label`, `reason` | Set new label; preserve old label as alias | reversible | Refuses if another active node of the same `node_type` already has `new_label` (error message tells the agent to use `kg_merge_nodes` instead); refuses locked rows |
| `kg_update_node_field` | `node_id`, `field`, `value`, `reason` | Single-field update | reversible | `field` must be in `_ALLOWED_UPDATE_FIELDS` allowlist (description, dates, importance, etc.); list-typed fields (`aliases`, `hash_tags`) require a `list_op` of `set` / `add` / `remove` so partial updates don't clobber; refuses locked rows |
| `kg_delete_edge` | `edge_id`, `reason` | Single edge removed | reversible (snapshot in `before_json`) | Refuses if the edge or either endpoint node is locked |
| `kg_repoint_edge` | `edge_id`, `reason`, ≥1 of `new_source_id` / `new_target_id` | Move one endpoint of an existing edge, keeping the row (id, sentence, evidence linkage) intact | reversible | The Disambiguation-attachment drain op. Refuses self-loops, no-op re-points, a re-point that would duplicate an existing `(source,target,rel)` edge, and any locked edge / endpoint (old or new) |
| `kg_split_succession` | `node_id`, `split_date`, `reason` | Era-split a role-reference Entity: close old era at `split_date`, mint same-label successor + a Disambiguation park point, chain `old --succeeded_by--> successor`, optionally repoint era-mismatched edges. Delegates to `kg_core/kg_utils/succession.py:split_succession` | reversible (revision log) | Refuses non-Entity nodes, Disambiguation nodes, locked nodes, already-closed eras, `split_date <= start_date` |
| `kg_set_user_lock` | `node_id`, `locked` (bool), `reason` | Stamp/clear `locked_by_user_at`; set `confidence_tier` to `axiom` (lock) or `provisional` (unlock) | reversible | The ONLY op permitted on a locked row (unlock is how the user revokes the guarantee — backs the KG Lens lock toggle). Refuses missing node + no-op transitions |
| `kg_delete_node` | `node_id`, `reason` | Delete node + explicitly delete all connected edges (no DB cascade) | partial — full node + edge snapshots in `before_json` | Refuses locked node, refuses node with any locked edge |
| `kg_create_state_node` | `owner_node_id`, `predicate`, `label`, `reason` (+ optional `node_type`, `category`, dates, etc.) | New State or Event node + owner→state edge, in one transaction | reversible (delete node + edge) | `node_type` restricted to `_ALLOWED_NEW_NODE_TYPES` (`State`, `Event`) |
| `kg_create_edge` | `source_id`, `target_id`, `relationship_type`, `reason` | New edge between existing nodes | reversible | Refuses self-edges, refuses if both endpoints don't already exist, refuses a duplicate `(source,target,rel)` edge |
| `kg_close_state` | `node_id`, `end_date`, `reason` | Set `end_date` (and optional prose) on a State / Event | reversible | Refuses if `node_type` not in `_ALLOWED_NEW_NODE_TYPES`; refuses if the node already has an `end_date` unless `force=true`; refuses locked rows |
| `kg_finding_resolve` | `finding_id`, `action`, `reason` (+ optional `notes`) | Sets finding `status='executed'`, records `executed_by` from scope.actor_id | reversible | Refuses if the finding is already terminal |
| `kg_finding_escalate` | `finding_id`, `summary`, `suggested_action`, `reason` | Sets finding `status='escalated'` | reversible | Refuses if the finding is already terminal |

**Common contract** across all handlers:

- Every call requires a non-empty `reason`. Empty `reason` raises `ValueError("reason is required for every mutation op.")`.
- `dry_run: true` returns the diff that would be applied without touching the KG — content is `"DRY RUN: would …"`, data carries `before` + `after` previews.
- Optional `finding_id` ties the revision back to its source finding; copied into both `args_json` and the dedicated `finding_id` column on `kg_revision_log`.
- Errors are surfaced as `ToolResult(data={"ok": False, ...})` rather than raised — the calling agent inspects and decides (typically: escalate the finding rather than retry).

## `kg_revision_log` table

`app/assistant/database/kg_revision_log.py`. One row per attempted
mutation. Reversibility lives here: each row carries enough state in
`before_json` / `after_json` to reconstruct the change.

| Column | Role |
|---|---|
| `op` | Mutation kind (see vocab below) |
| `args_json` | The arguments the agent supplied |
| `before_json` | Snapshot(s) of affected rows pre-mutation |
| `after_json` | Snapshot(s) post-mutation (or `"DELETED"` for deletions) |
| `reason` | Mandatory free-text justification — the audit trail |
| `finding_id` | Source finding row (when the mutation came from one) |
| `agent_id` | Free-form actor (`kg_finding_executor`, `ui::merge_form`, …) |
| `succeeded` | `1` on commit, `0` on attempted-but-failed (for retry / audit) |
| `error_message` | Error text when `succeeded=0` |
| `reverted_at`, `reverted_by` | Set when the change has been undone |

Reversal logic itself is not yet implemented — the snapshots make it
possible, but no `revert_revision()` function exists today. The
redundancy of `before_json` is what gives this loop its safety: even
an irreversible-in-spirit op like merge can be reconstructed from the
log.

## The `merge_nodes` FK-cascade trap

This is the single most important thing to internalize before adding a
mutator op that combines "rewrite FK references" with "delete the old
parent row." Without an explicit `session.flush()` between rewrite and
delete, SQLAlchemy's unit-of-work batches them, SQLite sees the delete
first, the FK cascade nulls out the still-pointing endpoints, and the
rewritten rows fail their NOT NULL constraint.

The shape that works (in `handle_kg_merge_nodes`):

```python
for e in in_edges_to_rewrite:
    e.target_id = keep_id
for e in out_edges_to_rewrite:
    e.source_id = keep_id
keep.aliases = new_aliases
# Flush BEFORE delete so the FK CASCADE on Edge.{source,target}_id
# sees edges already pointing at keep, not still-pointing-at-fold.
session.flush()
session.delete(fold)
```

If you're adding a new mutator (`split_node`, edge-target rewrites,
chained-merge variants), `flush()` between the rewrite and the delete
is load-bearing. Don't trust SQLAlchemy's implicit ordering to save
you.

The same trap applies in reverse for `kg_create_*` ops that need a
parent row to exist before a child FK fires — see
`handle_kg_create_state_node` (`session.flush()` after `session.add(new_node)`,
before the edge FK fires).

**Note — `kg_delete_node` no longer relies on a DB cascade.** The
DB-level `ON DELETE CASCADE` from `Edge.{source,target}_id` to `Node`
was **dropped 2026-05-10**, so `handle_kg_delete_node` deletes the
connected edges explicitly (snapshotting each first) before deleting the
node; without that explicit delete the edges would orphan, not cascade.

## Cookbook: adding a new mutator op

1. **Add `handle_<tool_name>` to `KGMutatorTool`.** It must:
   - Validate args; raise `ValueError` for invalid input.
   - Open one `get_db_manager().transaction(op="kg_mutator.<op>")` session.
   - Snapshot affected rows into `before` *before* mutating.
   - If you delete a row whose children FK-reference it, **`session.flush()`** between the children-rewrite and the parent delete (FK trap above).
   - Call `_write_revision_log(...)` with `op`, `args`, `before`, `after`, `reason`, `finding_id`, `agent_id` — every commit gets a row.
   - Return `ToolResult(data={"ok": True, "revision_log_id": rid, ...})`. On failure, `ok: False` with details.
   - Honour `dry_run: true` — return the would-apply diff without touching the DB.

2. **Add a thin tool-wrapper directory** under `app/assistant/lib/tools/<tool_name>/`:
   - `__init__.py`: `get_tool_class = create_tool_loader(KGMutatorTool)`
   - `tool_contract.json` (copy `kg_merge_nodes/tool_contract.json` as a template)
   - `prompts/<tool_name>_description.j2` and `prompts/<tool_name>_args.j2`
   - `tool_forms/tool_forms.py` for the Pydantic argument schema

3. **Register with the executor manager** — add the new tool name to all **three** lists in `app/assistant/multi_agents/kg_resolution_manager/config.yaml`: `tools.allowed_tools`, `scope_contract.tools.allowed_tools`, and `tool_visibility.always_show`. Missing any one of the three silently drops the tool (the allowlist is a ceiling, scope re-asserts it, and visibility decides what the selector sees).

4. **Tell the investigator about it** — update the `recommendation` examples in `app/assistant/agents/kg_investigation/planner/prompts/system.j2` so the prose-recommendation language includes the new op as an option.

## Cookbook: adding a new producer (writes findings)

1. **Pick a `finding_type` string.** Reuse an existing one if your producer detects the same class of problem; coin a new one if not.
2. **Call `upsert_finding`** from `app.assistant.kg_maintenance.store` with `finding_type`, `primary_node_id`, `suggested_action`, plus optional `secondary_node_id` / `edge_id` / `reason` / `confidence` / `priority` / `evidence_json` / `pipeline_run_id`. Dedup is automatic on `(finding_type, primary, secondary, status='pending')`.
3. **Add a brief branch** in `app/assistant/kg_investigator/finding_brief.py:build_finding_brief` for your `finding_type` — pre-fetch the obvious context (subject node, neighborhood, type-specific evidence) so the investigator's query budget goes to real digging instead of re-discovering basics.
4. **Set the `task_phrase`** to tell the investigator's planner what shape of recommendation you expect.
5. **For pipeline producers**, call `_investigate_findings_for_run(run_id)` (or pass your fresh ids to `investigate_findings`) at the end of your scan so the investigation pass is bounded to your sweep.

## Why no `kg_create_node` op in the autonomous executor

The wrappers under `app/assistant/lib/tools/kg_create_node/` and
`kg_update_node/` exist for the dev-room admin console
(`kg_dev_manager`), not for the autonomous executor. Generic node
creation from prose is too underconstrained for safe auto-apply —
nodes need a relationship to an existing subject, which is what
`kg_create_state_node` enforces structurally. The executor sees the
narrow tools; the dev console sees the full set.

## Key files

| Concern | Path |
|---|---|
| Mutator tool core (all handlers) | `app/assistant/lib/core_tools/kg_mutator/kg_mutator_tool.py` |
| Era-split executor (for `kg_split_succession`) | `app/assistant/kg_core/kg_utils/succession.py` (`split_succession`) |
| Audit log model | `app/assistant/database/kg_revision_log.py` |
| Tool wrappers | `app/assistant/lib/tools/kg_*/` |
| Findings store + upsert | `app/assistant/kg_maintenance/store.py` |
| Brief builder | `app/assistant/kg_investigator/finding_brief.py` |
| Executor manager (canonical caller) | `app/assistant/multi_agents/kg_resolution_manager/config.yaml` |
