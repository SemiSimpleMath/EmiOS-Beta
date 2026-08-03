# emi_team Pattern and ScopeContext

## Why this page exists

`emi_team_manager` is the prototype that almost every general-purpose worker manager in EmiOS is shaped from: planner-driven, tool-calling, with a critic and a periodic summarizer wrapped around it. Specialized managers (`kg_investigation_manager`, `kg_mutation_manager`, `devices_manager`, `entertainment_manager`, ...) reuse `emi_team`'s `delegator` and `summary` agents and swap in their own `planner` and `final_answer`. Read this page **before** [02_MANAGERS.md](02_MANAGERS.md) — once you see how derived managers compose, the rest of the manager docs are a list of variations on this one shape.

The second half of this page covers `ScopeContext`, the permission envelope every Message carries through the manager runtime. Scope is the load-bearing rule that keeps a "read-only investigator" from accidentally mutating the KG. The non-obvious rule — and the one that bites callers — is that a manager's `scope_contract` can only **narrow** what came in on the inbound Message; it cannot widen.

---

## The emi_team pattern

```
                          inbound Message
                                 |
                                 v
  +-----------+    +---------+    +---------+    +-------------+
  | delegator |--->| planner |--->|critic_pre|---|tool_caller  |
  +-----------+    +---------+    +---------+    +-------------+
       ^                ^              |               |
       |                |              v               v
       |                |        critic_post      tool_result_handler
       |                |              |               |
       |                |              v               v
       |                |         (back to planner)    tool_return_router
       |                |                              |
       |                |                              v
       |                |                          summary_pre_node
       |                |                              |
       |                +<-- (loop) ------+            v
       |                                  |       (cadence hit?)
       |                                  +<-- summary_post <- summary
       |
       |          planner returns "return_control"
       |                |
       |                v
       |          final_answer  -->  manager_exit_node
       |
       +<------ delegator routes every cycle by reading state_map
```

Per-agent roles:

| Agent | Class | Role |
|---|---|---|
| `emi_team::delegator` | `Delegator` | Pure router. Reads `state_map[last_agent]` (or an explicit `next_agent` override) and sets `next_agent` for the manager loop. No tools. Runs every cycle. |
| `emi_team::planner` | `Planner` | The brain. Picks the next action (tool name + arguments) or returns `return_control`. Maintains `checklist` + `progress` over the whole task. `gpt-5.1` smart-tier. |
| `emi_team::critic` | `PlaywrightAgent` | Synchronous guardrail in front of `tool_caller`. Inspects the planner's pending tool call and the recent history; if it spots a failure mode (modal blocking, looping, wrong tab, no progress), forces `must_revise_plan=true` so control bounces back to the planner before the tool runs. Returns `action="done"` only — never executes a tool itself. `gpt-5-mini`. |
| `emi_team::summary` | `Agent` | Periodic compression of execution history. Triggered by `summary_pre_node` on cadence (`cadence_every_actions`, `min_messages`, or large tool-result hits). Outputs `summary_pairs` keyed by `history_id`, plus hide/unhide/pin/delete lists. Never invents facts; never summarizes the latest tool result. `gpt-5-mini`. |
| `emi_team::final_answer` | `Agent` | Terminal compiler. Produces the structured report that the manager returns to the caller via `manager_exit_node`. `action_required: false` — no tool call expected. `gpt-5-mini`. |

The control nodes between the agents (`tool_caller`, `tool_result_handler`, `tool_return_router`, `critic_pre_node`, `critic_post_node`, `summary_pre_node`, `summary_post_node`, `manager_exit_node`, `graceful_exit_control_node`) are deterministic — they don't call LLMs. See [04_CONTROL_NODES.md](04_CONTROL_NODES.md).

Manager file: `app/assistant/multi_agents/emi_team_manager/config.yaml`.
Agent dir:    `app/assistant/agents/emi_team/{delegator,planner,critic,summary,final_answer}/`.

---

## Reusing emi_team in derived managers

Every derived manager (`kg_investigation_manager`, `kg_mutation_manager`, `devices_manager`, `entertainment_manager`, ...) declares the **same** delegator and summary agents from the `emi_team::` namespace, then swaps in its own planner and final_answer. The mechanism is plain agent-name reuse plus a `role_bindings` table.

From `app/assistant/multi_agents/kg_investigation_manager/config.yaml:5-17`:

```yaml
role_bindings:
  delegator: emi_team::delegator
  tool_selector: shared::tool_selector

agents:
  - name: emi_team::delegator           # reused
    class: Delegator
  - name: kg_investigation::planner     # specialized
    class: Planner
  - name: emi_team::summary             # reused
    class: Agent
  - name: kg_investigation::final_answer # specialized
    class: Agent
```

Two things to notice:

1. **`role_bindings.delegator` resolves at the loop entry.** `MultiAgentManager.run_agent_loop` calls `self.resolve_role_binding('delegator')` (`MultiAgentManager.py:510`) and then activates that agent every cycle. The literal binding `delegator: emi_team::delegator` is what makes "use the shared delegator" the default for every derived manager.
2. **The agent registry is global.** When a config lists `emi_team::delegator` it's pulling the same instance/loader the original `emi_team_manager` uses. There's no copy-paste; there's just a shared name.

The summary agent is reused the same way — listed in `agents:`, then referenced by the `summary_pre_node` and `summary_post_node` configs (`summary_agent: "emi_team::summary"`). Derived managers point their `resume_agent` at *their own* planner so summarization returns control to the right place.

The planner and final_answer are specialized because:

- The **planner** prompt and tool surface are domain-specific. `kg_investigation::planner` only knows about `kg_query`, `pod_search`, `pod_fetch`, `ask_user`. `kg_mutation::planner` only knows about typed mutator tools (`kg_merge_nodes`, `kg_rename_label`, etc.). The shared `emi_team::planner` would not know to refuse anything outside the report's `proposed_action`.
- The **final_answer** structured output differs per manager — kg_mutation needs to surface `op_applied`, `revision_log_id`, `finding_status` so `finding_executor._extract_outcome_from_audit` (`finding_executor.py:148-164`) can read the result; the generic `emi_team::final_answer` does not produce those fields.

The critic is *optional* in derivations. `kg_investigation_manager` and `kg_mutation_manager` drop it (no `critic_pre_node` / `critic_post_node` in `control_nodes`, no critic step in `state_map`). `entertainment_manager` keeps it because it does web/Playwright work where the critic earns its keep.

---

## Variations: what derived managers change

Across `emi_team_manager`, `kg_investigation_manager`, `kg_mutation_manager`, `devices_manager`, `entertainment_manager`, the differences cluster in five places:

| Knob | What it does | Example |
|---|---|---|
| `state_map` | Routing graph through the loop. Derived managers point `delegator -> <their planner>` and `<their planner>_return_control -> <their final_answer>`. They may also drop the critic transitions when not used. | `kg_investigation_manager/config.yaml:105-116` |
| `tools.allowed_tools` / `except_tools` | The runtime tool surface for *this manager invocation* (intersected with task spec restrictions in `ToolScopeService`). `[all]` opens everything; explicit lists lock down. | `emi_team_manager/config.yaml:58-60` is `[all]`; `kg_investigation_manager` lists 4 tools. |
| `scope_contract` | The manager's own narrowing of inbound `ScopeContext`. **Cannot widen.** Typically restates the tool allowlist + denylist and pins `writes.write_kg`. | `kg_investigation_manager/config.yaml:66-86`, `kg_mutation_manager/config.yaml:72-94`. |
| `tool_visibility` | What the planner *sees* in its prompt. `always_show` pins certain tools; `use_narrower: true` calls `shared::tool_narrower` to filter the rest; `hidden_tools` enumerates leaves to hide so the planner calls the manager wrapper instead. | `emi_team_manager/config.yaml:65-172` (very long). `kg_investigation_manager` keeps it small with `use_narrower: false`. |
| `flow_config.summary` | Cadence, minimum history depth, large-result threshold for `emi_team::summary`. Derived managers tune `cadence_every_actions`, `min_messages`, `trigger_on_large_result_chars` to their workload. | `devices_manager` uses tighter cadence (`cadence_every_actions: 3`); `emi_team_manager` uses `cadence_every_actions: 4`. |

Less common variations: `max_cycles` (16 for `kg_mutation_manager`, 80 for `emi_team_manager` and `entertainment_manager`), `flow_config.strict_routing: true` (forces `state_map`-only routing in the kg managers), `shared::compact_final_answer` substituted for `emi_team::final_answer` in `devices_manager` for terse confirmations.

---

## The state_map

Routing through the manager loop is **deterministic**. There's no LLM "what should I do next" decision — the delegator just looks up `state_map[last_agent]` and sets `next_agent`. From `MultiAgentManager._run_loop` (`MultiAgentManager.py:394-502`):

```python
pre_next_agent = self.blackboard.get_state_value("next_agent")
pre_last_agent = self.blackboard.get_state_value("last_agent")
delegator.action_handler(...)
next_agent_name = self.blackboard.get_state_value('next_agent')
route_source = (
    "explicit_override"
    if isinstance(pre_next_agent, str) and pre_next_agent.strip()
    else "state_map"
)
```

Two routing sources:

1. **`explicit_override`** — a control node or agent set `next_agent` directly on the blackboard *before* the delegator ran. Used by control nodes that need to short-circuit the table (e.g. `summary_pre_node` setting `next_agent` to `emi_team::summary` when cadence triggers, then `summary_post_node` resuming the planner).
2. **`state_map` lookup** — the default. The delegator reads `flow_config.state_map[last_agent]` and writes that as `next_agent`.

The state_map is a string-to-string mapping. Each key is an agent or control-node name (the *previous* step), each value is the next step. Special keys:

- `"<planner_name>_return_control"` — the planner can output `action: return_control` to signal task completion. The delegator sees `last_agent == "<planner_name>_return_control"` and routes to `final_answer`.
- `"graceful_exit"` — set by `_run_loop` exit paths (`max_cycles`, `error`, unknown). Routed through `graceful_exit_control_node` to produce a structured abort report rather than a clean final answer.

The validator `_validate_strict_routing_config` (`MultiAgentManager.py:55-115`) fails fast if `state_map` is missing/empty or references nonexistent control nodes, so misconfigurations show up at boot, not mid-loop.

`last_agent` is set after every agent activation. `next_agent` is consumed by the delegator and cleared (or overwritten) each cycle.

---

## ScopeContext: the permission envelope

`ScopeContext` (`app/assistant/utils/pydantic_classes.py`, `schema_version="scope_context_v1"`) is the Pydantic struct attached to every `Message` that crosses a manager or pipeline boundary. It carries identity (who's calling, on what surface, in what room) and a set of sub-policies that govern what the call can *do*.

Top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `Literal["scope_context_v1"]` | Pinned version. |
| `scope_id` | `str` | Unique id for this scope instance (used in audit trails). |
| `owner_id` | `str` | Whose data is in scope. Typically the user's id (`"alex"`); for room-derived scopes, the `room_id`. KG queries use this for owner-scoping. |
| `actor_id` | `str` | Who *initiated* the call. User id for chat; agent or pipeline name for system-driven work. |
| `surface` | `str` | Transport surface: `"ui"`, `"slack"`, `"telegram"`, `"sms"`, `"system"`, `"pipeline"`, ... Used to choose default authority and reply types. |
| `room_id` | `Optional[str]` | Room when applicable; `None` for pipeline / system scopes. |
| `room_context_id` | `Optional[str]` | Sub-context within a room (default `"main"`). |
| `visibility` | `"owner_only" \| "room_shared" \| "global_shared"` | Who can read messages produced under this scope. |
| `policy_id` | `Optional[str]` | Reference to a named policy (`room_policy::master_room::v1`, `pipeline_policy::kg_chat_pipeline::v1`). |

Sub-policies (each a `ScopeBaseModel` with `extra="forbid"`):

| Sub-policy | Notable fields |
|---|---|
| `history` (`ScopeHistoryPolicy`) | `mode` (none / recent_only / summary_plus_recent), `source` (scope_local / unified_log), `lookback_hours`, `max_messages`. |
| `resources` (`ScopeResourcePolicy`) | `allowed_global_resources` (or `["all"]`), `allowed_room_resources`, `denied_resources`, `resource_groups` (RAG scopes). |
| `tools` (`ScopeToolPolicy`) | `allowed_tools` (`["all"]` or explicit list — cannot mix; a field validator rejects `["all", "x"]`), `blocked_tools`, `requires_approval_tools`, `allow_external_side_effects`, and `per_manager` (`Dict[str, ScopeToolRule{allow, block}]` — fires when the named manager runs anywhere in the call tree; the primary surgical lever). |
| `pods` (`ScopePodPolicy`) | `allowed_scopes` — which pod `scope_id`s this scope may read; `["self"]` default (own room only), `["all"]` for owner cross-room surfaces. Sensitivity/`min_authority` is a separate axis. |
| `entities` (`ScopeEntityPolicy`) | `enabled`, `allowed_entity_cards`, `pinned_entities`, lookback windows. |
| `cards` (`ScopeCardPolicy`) | `allowed_cards`, `max_cards_per_turn`, `max_total_chars`. |
| `writes` (`ScopeWritePolicy`) | `write_unified_log` (default `True`), `write_kg` (default `False`), `allow_fact_extraction` (default `False`), `writable_state_keys`. |
| `delivery` (`ScopeDeliveryPolicy`) | `auto_send`, `allow_initiation`, `allowed_reply_types`. |
| `approval` (`ScopeApprovalPolicy`) | `authority_level: int` 0..100. Higher means more trusted. |
| `retention` (`ScopeRetentionPolicy`) | `persist_chat`, `persist_tool_results`, `allow_context_summarization`, `redact_before_persist`. |
| `execution` (`ScopeExecutionPolicy`) | `max_turns`, `max_tool_calls`, `timeout_seconds`, `allowed_models`. |
| `delegation` (`ScopeDelegationPolicy`) | Currently empty — placeholder. |

Where scope is built (`ScopeAdapter` in `scope_adapter.py`):

- **Inbound user messages from a room**: `_derive_room_scope` delegates to the single reference builder `build_scope_contract_for_room_request` (room_scope_builder) so the chat and system ingress vectors produce *identical* permission for the same room.
- **System / background work**: `build_system_scope_context`.
- **Pipelines**: `load_scope_for_source(kind="pipeline", source_id=...)` (`app/assistant/scope/loader.py`) — loads the pipeline's `scope.yaml` and stamps per-run identity. Built once at run start, threaded through every step.
- **Explicit caller-provided scopes**: handed to `ManagerInvoker.invoke` on the `Message`, then passed through `ScopeAdapter.apply` for narrowing.

`ScopeAdapter.apply` is what `ManagerInvoker` calls before `request_handler`. It resolves the scope (`_resolve_scope_context`: inherit from message → derive from room → system floor), applies the manager's `scope_contract` *as a narrowing* (`_apply_manager_narrowing`), and projects the result onto `message.data` so legacy tool-scoping code can keep reading `task_allowed_tools`, `write_kg`, etc. (`_project_scope_to_runtime_data`).

**Strict ingress (default).** When a request reaches `_resolve_scope_context` with *no* inbound scope, *no* room, and *no* `data.scope_contract`, the adapter **refuses** rather than silently synthesizing a wide system scope. `_strict_scope_enabled()` returns `not test_mode` by default (an explicit `SCOPE_CONTRACT_STRICT` env always wins; `EMI_TEST_MODE=1` / `PYTEST_CURRENT_TEST` relax it to *derive*, mirroring `MultiAgentManager`'s own test-mode substitution). Every live invoker path (chat, dayflow, maintenance, task) already attaches a scope or room, so a scope-less production ingress is an unintended ungated invocation and fails loud. (A narrow exception: `_allow_strict_mode_system_derivation` still derives when the request carries a `task_file`, a `scope_contract` seed, or a resource contract.)

---

## The narrowing-only rule (ceiling, not per-level grant)

A manager's `scope_contract` block in its `config.yaml` can only **tighten** the inbound scope — never loosen it. But "tighten" is **not** a blind intersection for the tool surface.

`ScopeAdapter._apply_manager_narrowing` treats the inbound `allowed_tools` as a **ceiling** and resolves the manager's *own* surface (`scope_contract.tools.allowed_tools` if it declares one, else the manager's `config.tools.allowed_tools`) against it:

- **parent `["all"]`** → the manager's own surface stands verbatim.
- **`manager_name` ∈ the parent's allow-list** → the parent **granted** this manager, so its *whole own surface* is allowed (granting a manager grants its subtree — **not** an intersection with the parent's narrowed leaf list).
- **otherwise** → bounded by the ceiling (intersection); an empty parent → `[]` (allow nothing → the breach wall holds).

This is the **starvation fix**: a sub-manager reached through a narrowed parent (`master_room → emi_team → sandbox`) used to inherit the parent's small leaf allow-list and **lose its own tools**, because only a declared `scope_contract.allowed_tools` triggered the grant. Reading the config surface plus the "granted ⇒ whole subtree" rule is what keeps a granted sub-manager from being starved.

The rest of the narrowing is monotone:

- `tools.blocked_tools` / `requires_approval_tools` → set-union with inbound (additive denial).
- `tools.allow_external_side_effects` → may flip `True`→`False`, never `False`→`True`.
- `resources.allowed_global_resources` / `allowed_room_resources` → intersected.
- `entities.enabled` → may flip `True`→`False`, never reverse; `allowed_entity_cards` / `pinned_entities` → intersected.
- `writes.{write_unified_log, write_kg, allow_fact_extraction}` → may flip `True`→`False`. **Flipping `False`→`True` raises `ValueError`.**
- `approval.authority_level` → may *clamp down* but raises `ValueError` if `requested > parent`.

### `per_manager` — the primary surgical lever (and switchboard-locking)

After the ceiling logic, a **scope-level** `tools.per_manager[<manager_name>]` rule (`ScopeToolRule{allow, block}`) is folded into `allowed_tools` so it binds at **execution** (`tool_access_control.check_tool_access` reads `allowed_tools`), not merely at visibility. `allow` (when present, even empty) **replaces** the surface when the ceiling is `["all"]`, else intersects it; `block` is subtracted from the surface **and** unioned into `blocked_tools` so it propagates to children.

This is the lever that survives a room overwriting `allowed_tools`: a room's own `allowed_tools` is *replaced* by its manager's `scope_contract`, so the surviving way to restrict what a room's switchboard may route to is a `per_manager` rule keyed on the **hosting manager** (`room_manager` for non-master rooms — not the room id, not the agent name). See SCOPE.md §6 for the wired switchboard example.

The error a caller will see on a widening attempt:

```
ValueError: [<manager_name>] scope_contract attempted to expand writes.write_kg
            from false to true.
```

or:

```
ValueError: [<manager_name>] scope_contract attempted to expand approval.authority_level
            from <parent> to <requested>.
```

The fix pattern: when a caller wants a manager to do something more permissive than the inbound message allows, **the caller** has to seed an explicit, permissive `ScopeContext` on the `Message` before invoking. The manager's own `scope_contract` then narrows back down to the typed surface it actually wants — keeping the audit trail honest while not blocking work.

> Note: `requires_approval_tools` is interesting — additive narrowing means a manager can *add* approval requirements over the inbound scope, but cannot *remove* them. This matches the rest of the model.

---

## Case study: kg_finding_executor and `finding_write_scope()`

`kg_finding_executor.run_executable_findings` (`app/assistant/kg_investigator/finding_executor.py`) is a routine-driven sweeper that picks up `kg_maintenance_finding` rows whose investigator already proposed an action and hands each one to `kg_mutation_manager` for application. It's a system-initiated call with no inbound user `Message`.

**The bug.** The first wiring used `Message(task=..., information=...)` with no `scope_context`. `ScopeAdapter` fell into `_derive_system_scope`, which builds a scope where `writes.write_kg` defaults to `False`. `kg_mutation_manager`'s `scope_contract` says `writes.write_kg: true` — and `_apply_manager_narrowing` saw that as the manager *widening* `False` → `True` and raised:

```
ValueError: [kg_mutation_manager] scope_contract attempted to expand writes.write_kg
            from false to true.
```

**Diagnosis.** The manager's `scope_contract` is not the place to *grant* the right; it's the place to *constrain* it. Granting has to happen at the call site, where the system (not a user message) knows it has authority to mutate.

**The fix.** A `finding_write_scope()` helper (now in `finding_processor.py`, shared by the executor and the resolution manager) builds the permissive scope and attaches it to the outbound Message. Note the *current* shape: it no longer hand-rolls a `ScopeContext(...)` — that would be an anti-pattern (SCOPE.md §12). It goes through the single loader, reading `kg_investigator/scope.yaml`:

```python
def finding_write_scope() -> ScopeContext:
    return load_scope_for_source(
        kind="subsystem",
        source_id="kg_investigator",
        actor_id="kg_finding_executor",
        identity_overrides={
            "owner_id": PRINCIPAL_USER,
            "actor_id": "kg_finding_executor",
            "scope_id": "scope::kg_investigator::finding_executor",
        },
    )
```

and is attached on the message: `Message(task=..., information=..., scope_context=finding_write_scope())` → `DI.manager_invoker.invoke(mgr, msg)`.

The manager's own `scope_contract` still does its job — `kg_mutation_manager` narrows the tool surface to `kg_query`, `kg_merge_nodes`, `kg_rename_label`, `kg_update_node_field`, `kg_repoint_edge`, `kg_create_state_node`, `kg_close_state`, `kg_finding_resolve`, `kg_finding_escalate`, `ask_user` and explicitly blocks the raw `kg_create_node` / `kg_create_edge` / `kg_delete_node` / `kg_delete_edge` / `kg_update_node` family (plus `install_tool`). The narrowing constrains, not grants.

This is the canonical pattern. Any system-initiated caller that needs to grant something the default `_derive_system_scope` does not (KG writes, fact extraction, raised authority level, broader resource access) builds a scope at the call site — preferably via `load_scope_for_source` against the caller's own `scope.yaml`, as `finding_write_scope()` now does.

---

## Authority levels

`ScopeApprovalPolicy.authority_level` is an `int` 0..100. Higher = more trusted. It's used by approval gates to decide whether a particular action needs a human ack.

Defaults flow from surface (`app/assistant/utils/surfaces.py:13-17`):

```python
DEFAULT_AUTHORITY_BY_SURFACE: dict[str, int] = {
    SURFACE_SLACK: 50,
    SURFACE_TELEGRAM: 40,
    SURFACE_SMS: 40,
}
```

Other surfaces default to `0`. Rooms can override via `room_policy.authority_level`; `master_room` sets `99` (`app/assistant/rooms/master_room/ROOM.md` frontmatter — the loose `policy.json` beside it is legacy and unread), reflecting that the local UI is the owner's primary control surface.

System-initiated background work (pipelines, routines, the finding executor) typically uses `100` because it represents the user's own automation. The narrowing rule still applies — a manager cannot *raise* the level above what the inbound scope grants.

> Note: only `authority_level` is currently clamped per-manager; other approval behavior (dangerous-tool gates, install gates) is not configurable in the narrowing.

### The L1 per-tool authority floor

Authority is not only an *approval* dial — it is also a **see+use floor** enforced at the access layer. Each first-party tool contract may carry `metadata.min_authority` (an int 0–100; `tool_access_control.resolve_tool_min_authority` reads it, MCP/dynamic tools have none). `check_tool_access` rejects the call when `scope.approval.authority_level < min_authority` (authority `>= 100` clears every floor; `None` = no floor, ceiling-gated only). This is the wall the Telegram breach lacked: a 40-authority guest could reach `personal_admin_manager` (floor 90) because nothing checked authority at the *access* layer — only the approval gate, which guests never tripped. `approval_min_authority` (in `tool_approval.py`) is the separate L2 dial that decides whether a permitted call still needs a human ack.

---

## Resource scoping

`ScopeResourcePolicy.allowed_global_resources` controls which resources from the global ResourceManager an agent can read. The convention is:

- `["all"]` — wildcard, no filtering. The default for room-derived scopes when the room policy doesn't restrict.
- `[]` — no rights. The agent cannot read any global resource.
- Explicit list — only the named resources are visible.

`allowed_room_resources` works the same way for room-scoped resources. `denied_resources` is a hard blocklist regardless of allow lists. `resource_groups` names RAG scopes for retrieval pipelines.

Pipelines that need a deliberately small resource surface ship a `scope.yaml` next to the pipeline that names exactly the resources they need; `load_scope_for_source(kind="pipeline", source_id=...)` loads it.

The context injector reads these lists when resolving `resource_*` context items in agent configs — see [01_AGENTS.md](01_AGENTS.md).

---

## Tool scope filtering

`ToolScopeService` (`app/assistant/manager_runtime/services/tool_scope_service.py`) is what consumes the `task_allowed_tools` / `task_except_tools` / `visible_tools` keys that `ScopeAdapter._project_scope_to_runtime_data` puts on `message.data`. It then applies the manager's `tool_visibility` config (`always_show`, `use_narrower`, `hidden_tools`) and, if a compiled task step pinned exact tools, bypasses everything else. (Because `per_manager` now folds into `allowed_tools` at narrowing time, visibility *derives from* the execution surface rather than re-deriving `per_manager` itself.)

End-to-end tool resolution order is covered in [07_TOOLS.md](07_TOOLS.md#tool-visibility-and-narrowing). The relevant point for this page: **scope is the upstream gate, visibility is the downstream filter.** Scope decides what the manager *may* call; visibility decides what its planner *sees*.

---

## Key files

| File | Purpose |
|---|---|
| `app/assistant/multi_agents/emi_team_manager/config.yaml` | The prototype manager. |
| `app/assistant/agents/emi_team/{delegator,planner,critic,summary,final_answer}/` | Reusable agent configs + prompts. |
| `app/assistant/multi_agents/kg_investigation_manager/config.yaml` | Read-only KG investigator. Reuses `emi_team::delegator` and `emi_team::summary`. |
| `app/assistant/multi_agents/kg_mutation_manager/config.yaml` | KG mutator. Pins `writes.write_kg: true` in `scope_contract`. |
| `app/assistant/multi_agents/devices_manager/config.yaml` | Smart-home worker. Uses `shared::compact_final_answer` instead of the long form. |
| `app/assistant/multi_agents/entertainment_manager/config.yaml` | Research worker; keeps the critic. |
| `app/assistant/manager_classes/MultiAgentManager.py` | Base class: agent loop, role resolution, blackboard scope context handling. |
| `app/assistant/manager_runtime/manager_invoker.py` | Calls `ScopeAdapter.apply` then `request_handler`. |
| `app/assistant/manager_runtime/services/scope_adapter.py` | Builds, derives, and narrows `ScopeContext`. Ceiling-narrowing + `per_manager` folding + strict ingress + widening-rejection. |
| `app/assistant/lib/tool_execution/tool_access_control.py` | Execution gate: `check_tool_access` (allow/block + the L1 `min_authority` floor). |
| `app/assistant/manager_runtime/services/tool_scope_service.py` | Downstream tool visibility filter. |
| `app/assistant/utils/pydantic_classes.py` | `ScopeContext` and all sub-policies (`ScopeToolPolicy.per_manager`, `ScopePodPolicy`, …). |
| `app/assistant/utils/surfaces.py` | Surface constants and `DEFAULT_AUTHORITY_BY_SURFACE` (slack 50 / telegram 40 / sms 40). |
| `app/assistant/scope/loader.py` | `load_scope_for_source(kind=…)` — the single loader for source-owned `scope.yaml`. |
| `app/assistant/kg_investigator/finding_processor.py` | `finding_write_scope()` — the case study. |

---

## Cookbook: derive a new manager from emi_team

1. Create `app/assistant/multi_agents/<your_manager>/config.yaml` and `__init__.py`.
2. Set `class_name: MultiAgentManager`. Pick a `max_cycles` that matches the workload (16 for narrow tasks, 80 for open-ended research).
3. Reuse the shared agents in `agents:`:
   - `emi_team::delegator` (Delegator)
   - `emi_team::summary` (Agent)
   - Optionally `emi_team::critic` (PlaywrightAgent) if the work involves browser/web action.
4. Add your specialized agents under their own namespace, e.g. `your_namespace::planner` and `your_namespace::final_answer`. Put their configs/prompts under `app/assistant/agents/your_namespace/`.
5. Set `role_bindings.delegator: emi_team::delegator` and `role_bindings.tool_selector: shared::tool_selector`.
6. List the control nodes you actually use. The required minimum for a tool-calling manager is `tool_caller`, `tool_result_handler`, `tool_return_router`, `manager_exit_node`, `graceful_exit_control_node`. Add `summary_pre_node` + `summary_post_node` if you want compression. Add `critic_pre_node` + `critic_post_node` only if you reuse the critic.
7. Write `flow_config.state_map`. Required edges:
   - `"emi_team::delegator": "<your::planner>"`
   - `"<your::planner>": "tool_caller"` (or `"critic_pre_node"` if using the critic)
   - `"tool_result_handler": "tool_return_router"`
   - `"tool_return_router": "summary_pre_node"` (if summarizing) or directly to your planner
   - `"<your::planner>_return_control": "<your::final_answer>"`
   - `"<your::final_answer>": "manager_exit_node"`
   - `"graceful_exit": "graceful_exit_control_node"`
   - `"graceful_exit_control_node": "<your::final_answer>"`
8. Set `tools.allowed_tools` and `except_tools` for the runtime tool surface.
9. Set `scope_contract` to the *narrowest* subset of permissions the manager needs. Remember it can only narrow.
10. Set `tool_visibility` (`always_show`, `use_narrower`, `hidden_tools`) for what the planner *sees*.

`_validate_strict_routing_config` will fail the boot if `state_map` references a control node you forgot to declare, so iterate against the validator.

---

## Cookbook: wire a caller that needs to grant `write_kg` / `write_unified_log` / raised authority

The default scope a `ManagerInvoker` synthesizes for a system-initiated call (no user message, no room) has `write_kg: false`, `allow_fact_extraction: false`, and `authority_level: 0`. If your caller needs more, build a permissive scope at the call site and attach it to the outbound Message.

```python
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeWritePolicy,
)

def _my_scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::<caller>::<purpose>",
        owner_id="primary_user",
        actor_id="<caller_name>",
        surface="system",
        room_id=None,
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(
            write_kg=True,            # only if the manager needs it
            write_unified_log=True,
            allow_fact_extraction=False,
        ),
    )

msg = Message(
    task="...",
    information="...",
    scope_context=_my_scope(),
)
DI.manager_invoker.invoke(mgr, msg)
```

Rules of thumb:

- Grant the **minimum** the target manager needs. The manager's own `scope_contract` will narrow further, but if you grant too much you weaken the audit story.
- `surface="system"` is the right marker for non-user-initiated work. `actor_id` should name the caller distinctly so logs and KG provenance can trace back to the right routine.
- For a pipeline, prefer `load_scope_for_source(kind="pipeline", source_id=..., actor_id=...)` and put the policy in `app/assistant/pipelines/<pipeline_id>/scope.yaml` — that way the policy lives with the pipeline definition rather than in inline code.
- For routine-style work calling into a manager that requires a permission the system default does not grant, follow the `finding_write_scope()` pattern (`kg_investigator/finding_processor.py`) — build the scope via `load_scope_for_source` against the caller's own `scope.yaml`.
