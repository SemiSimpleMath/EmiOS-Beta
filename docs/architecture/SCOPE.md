# How Scope Works (end-to-end reference)

> Audience: an engineer or agent who needs to understand, use, or modify EmiOS's
> scope system. This is the canonical "how it actually works" doc. The terse design
> decisions live in the `project_scope_*` / `reference_scope*` auto-memories; this doc
> is the long form. Last substantial update: 2026-05-31.

---

## 0. TL;DR

**Scope is a per-execution capability + identity envelope (`ScopeContext`) that travels on a `Message`.** Every LLM/tool execution runs under exactly one effective scope. The scope answers, for this run: *who is acting* (identity/provenance), *what may it touch* (tools, resources, pods, writes), and *how much is it trusted* (authority, approval).

The one rule that governs everything:

> **A caller-provided scope supersedes, and can only narrow as it flows. If no caller provides one, the execution's own *source* originates it. If neither exists, a locked-down system floor applies.**

Two invariants make that safe:
1. **Narrow-only**: scope can only get *more* restrictive as it propagates down a call tree. Nothing can widen what it was handed (a callee can never out-reach its caller).
2. **The `Message` is the boundary**: scope flows *only* when a caller passes a `Message` carrying `scope_context`. A plain Python call propagates nothing — the callee self-scopes or floors.

---

## 1. The two axes every component sits on: SOURCE vs CONSUMER

This is the most important mental model. Two *independent* properties:

| | **Source** | **Consumer** |
|---|---|---|
| Definition | Owns a `scope.yaml` / a real identity; can **originate** a scope | **Runs under** a scope; can **never** originate one |
| Examples | rooms, pipelines, tasks, subsystems, (some) routines | managers, agents, tools, tools' inner LLMs |
| Where its scope comes from | its own `scope.yaml` (built at its entry) | the inbound `Message`, or the source that hosts/triggers it |

Many components are **both** (a pipeline owns a scope.yaml *and* runs under it). The components that are **pure consumers** — **managers, agents, tools, inner LLMs** — are the trap: they have *no scope of their own* and **must be supplied** one.

- A **manager** has only a *narrowing* `scope_contract` in its `config.yaml` — **not** a `scope.yaml`. It can tighten an inbound scope but cannot create one. Its scope comes from the inbound `Message`, or (when invoked from a room) from its **room** (the room is the source).
- An **agent** runs under whatever scope is on the `Message` it's handed.
- A **tool** receives the caller's scope on `tool_message.scope_context`; if it runs an inner LLM, it must **thread that scope down**.

> "The callee self-scopes" is only true for *sources*. A manager/agent/tool never self-scopes — it inherits or is hosted.

---

## 2. The resolution precedence (how the effective scope is chosen)

For any execution, in order:

1. **Inbound caller scope** — a `Message` carrying `scope_context`. **Supersedes.** Identity (`owner_id`/`actor_id`/`acting_as`) flows down; permissions may only **tighten**. This is the *secure default* — the callee cannot exceed its caller.
2. **Else the hosting / triggering SOURCE originates it** — a manager's host is its **room**; an autonomous run's host is its **pipeline** or **routine**. (Pure consumers are never their own tier-2.)
3. **Else the system floor** — `build_system_scope_context(...)` produces a deliberately locked-down scope. This is the catch-all for "nothing attached + sourceless payload"; it must stay restrictive.

**The one deliberate exception: the courier path.** `ScopeAdapter.for_courier_call(...)` mints a band-100 scope so *deterministic Python* can unseal a secret that no LLM caller has. It intentionally grants *more* than the caller — but it is **not** an LLM caller→callee boundary, so it sits outside rule #1 by design. See §9.

This precedence is exactly what `ScopeAdapter._resolve_scope_context` already implements at manager ingress: *inherited Message scope → `_derive_room_scope` → `_derive_system_scope` (floor)*.

---

## 3. What a `ScopeContext` actually contains

`ScopeContext` (in `app/assistant/utils/pydantic_classes.py`, `schema_version="scope_context_v1"`) is a Pydantic model = **identity fields** + a set of **sub-policies**. Conceptually it splits into three *buckets* (see `reference_scopecontext_field_buckets` memory):

### Bucket A — PERMISSION (this is what a `scope.yaml` declares)
- `tools` (`ScopeToolPolicy`): `allowed_tools`, `blocked_tools`, `requires_approval_tools`, `allow_external_side_effects`, and `per_manager` (a dict of `ScopeToolRule{allow, block}` that fires for a named manager *anywhere* in the call tree).
- `resources` (`ScopeResourcePolicy`): `allowed_global_resources`, `allowed_room_resources`, `denied_resources` (hard blocklist), `resource_groups` (RAG scopes).
- `pods`: `allowed_scopes` (e.g. `[self]`).
- `entities`, `cards`: KG entity/card visibility.
- `writes` (`ScopeWritePolicy`): `write_unified_log` (default `True`), `write_kg` (default `False`), `allow_fact_extraction` (default `False`), `writable_state_keys`.
- `approval` (`ScopeApprovalPolicy`): `authority_level: int` (0..100).
- `delivery` partial: `auto_send`, `allow_initiation`.
- skills grant.

### Bucket B — ROOM-BEHAVIOR (NOT in scope.yaml; lives at the room layer)
- `history.*` (chat-history injection — single reader, `chat_gate` opt-in; inert for pipelines).
- `delivery.allowed_reply_types` (which transports a room may reply on).
- Also `retention`, `execution` (`max_turns`, `max_tool_calls`, `timeout_seconds`, `allowed_models`), `delegation` (placeholder).

### Bucket C — IDENTITY (stamped per request; a file may NOT author these)
`scope_id`, `owner_id`, `actor_id`, `room_id`, `room_context_id`, `reply_to`, `acting_as`, `policy_id`. (`surface` and `visibility` are stable source-ish props a file *may* declare, but identity can still override.)

> **Rule:** a `scope.yaml` declares **Bucket A only**. Identity (C) is stamped at load time by the caller; if a file authors identity keys they are dropped with a warning. Don't put `owner_id`/`actor_id`/`room_id` in a `scope.yaml`.

`Message.scope_context` is `Optional[ScopeContext]`. Note: on the blackboard it is stored **model_dumped to a dict**, but agent `Message`s carry the **object**. Readers must branch on `isinstance` — `getattr(dict, "field", default)` returns the default, not the dict key (see `feedback_scope_context_dict_or_object`).

---

## 4. The unified loader — `app/assistant/scope/loader.py`

There is **one** way to build a scope from a declaration. Everything converges here.

### `load_scope(source, *, identity) -> ScopeContext`
- `source` = a path to a `scope.yaml`, **or** an already-parsed mapping (dict).
- `identity` = the per-request envelope to stamp. MUST include `owner_id`, `actor_id`, `surface`. MAY include `scope_id` (generated if absent), `room_id`, `room_context_id`, `reply_to`, `acting_as`, `policy_id`, `visibility`.
- It strips any identity keys found in the file (warns), applies the **fail-closed floor**, stamps identity, validates.

### `load_scope_for_source(*, kind, source_id, actor_id, identity_overrides=None) -> ScopeContext | None`
Resolves a source's `scope.yaml` path by `(kind, source_id)`, builds a default identity, and delegates to `load_scope`. Build it **once** at the start of a run and **thread it** through every step — do not call per-step.

| `kind` | resolves to | notes |
|---|---|---|
| `pipeline` | `app/assistant/pipelines/<id>/scope.yaml` | `surface="pipeline"`; fail-loud if missing |
| `room` | `resolve_room_config_dir(<id>)/scope.yaml` | caller supplies the real transport `surface` |
| `subsystem` | `_SUBSYSTEM_SCOPE_DIRS[<id>]/scope.yaml` | registry map; fail-loud on unknown id |
| `routine` | `configs/routines/public/<id>.scope.yaml` | **OPTIONAL → returns `None` if the file is absent** (see §8) |
| `job` | — | `NotImplementedError` (not wired) |

### The fail-closed floor
A declaration that omits `tools.allowed_tools` gets an **empty** allowed surface (`[]`), **not** the permissive `["all"]` model default. Grants exist only where the source *explicitly* writes them. (`["all"]` still serves deliberately-permissive scopes built in code — courier, system — but the *file* path is fail-closed.)

---

## 5. The runtime path — manager ingress (`ScopeAdapter`)

`app/assistant/manager_runtime/services/scope_adapter.py`. The `ScopeAdapter` class is **instantiated by `manager_invoker.py`** and `.apply(...)` runs on **every manager invocation**. This is the live scope ingress and is load-bearing — do not treat it as "legacy."

```
manager_invoker.invoke(manager, message)
  └─ ScopeAdapter.apply(manager_name, manager_config, message)
       ├─ _resolve_scope_context(...)        # precedence (§2):
       │     inbound Message scope            #   tier 1: inherit (caller-supersedes)
       │       else _derive_room_scope(...)   #   tier 2: room is the source
       │       else _derive_system_scope(...) #   tier 3: build_system_scope_context floor
       ├─ _apply_manager_narrowing(...)       # the four-layer narrowing (§6), narrow-only
       └─ _project_scope_to_runtime_data(...) # stamps task_allowed_tools / write_kg / etc.
                                              # onto message.data + scope_contract_enforced
```

`_project_scope_to_runtime_data` exists so downstream legacy tool-scoping code can keep reading `task_allowed_tools` etc. off `message.data`. `ToolScopeService` (`tool_scope_service.py`) then consumes those keys for tool **visibility** filtering.

Other `ScopeAdapter` seams:
- `for_sub_manager(...)` — manager-as-tool delegation; returns the parent scope **verbatim** (the child then narrows via its own contract). This is the inheritance seam.
- `for_courier_call(...)` — the courier exception (§9).
- `build_system_scope_context(...)` — the system-scope builder behind the `_derive_system_scope` floor (and the not-yet-wired Orchestrator). Kept.

---

## 6. The four-layer tool gate

A tool call is allowed only if it passes **all four**:

1. **Allow/Block** (the grant): `scope.tools.allowed_tools` (with `["all"]` wildcard) minus `blocked_tools`. Enforced in `tool_access_control.check_tool_access`.
2. **`per_manager` rules**: `scope.tools.per_manager[<manager>]` = `ScopeToolRule{allow, block}` — a *flat* dict that fires when that manager appears **anywhere** in the call tree. Fail-closed, surgical narrowing of a specific manager's tool surface.
3. **Authority**: `scope.approval.authority_level`. In `tool_approval.py`, `authority_level >= 100` is an **admin bypass**; a tool's `approval_min_authority` gates whether it needs human approval.
4. **Approval**: `requires_approval_tools` + per-tool risk → `compute_approval_reasons`. If reasons exist and authority is insufficient, the call needs human approval.

**Visibility ≠ permission.** `ToolScopeService` narrows what the planner *sees* (downstream filter); the four gates decide what it *may call* (upstream gate). Visibility is never a permission lever — `allowed_tools` is the only grant. (Historic `always_show` "narrower-only" cleanup: do **not** re-add `or t in always_show_set` to any gate.)

> Known fail-open seam (tracked): an empty `allowed_tools` is mishandled in one visibility path in `tool_scope_service.py` — treat "empty means nothing" everywhere. Security follow-up.

---

## 7. The narrowing-only rule (managers)

A manager's `scope_contract` block in its `config.yaml` can **only tighten** the inbound scope, never loosen it. `ScopeAdapter._apply_manager_narrowing` does:
- `allowed_tools` → **intersected** with inbound (with `["all"]` semantics).
- `blocked_tools` / `requires_approval_tools` → **set-union** with inbound (additive denial).
- `allow_external_side_effects` → may flip `True`→`False`, never `False`→`True`.
- `resources.*` → intersected; authority → clamped down.

Consequence: anything in a manager's `allowed_tools` is **hidden + blocked at the `emi_team` layer** for sub-managers (the child inherits the parent's restriction and strips its own native tools). Don't forget `master_room_manager.allowed_tools` when reasoning about this.

---

## 8. Routines & the cron path

`RoutineManager` (`app/assistant/routine_manager/routine_manager.py`) is EmiOS's cron. Routines live in `configs/routines/public/<id>.json` and each declares a **`runner`** (one of `tool | task | job | function | pipeline`) + a `spec` + a `run_policy`. The runner dispatches the payload; the routine is the *trigger*, not the work.

**A routine is a trigger; a pipeline is one of five payload types it can dispatch.** They are orthogonal — don't conflate them. `dayflow_pipeline.json` is a *routine* (`runner: pipeline`, `spec.pipeline_id: dayflow`) that schedules the `pipelines/dayflow/` *pipeline*.

### Scope for the cron path (the optional-attach model)
Per the SOURCE/CONSUMER model, scope attaches to the **work unit**:
- `runner: pipeline | task | job` → the payload is a **source**; it self-scopes from its own `scope.yaml`. The routine attaches **nothing**. **Do not** give a pipeline a routine-level scope — it already has `pipelines/<id>/scope.yaml`; a second one is duplication.
- `runner: tool | (bare) function` → the payload is a sourceless **consumer**, so the **routine optionally supplies** the scope.

Mechanism (wired 2026-05-31, commit `a05a6e0a`):
- `RoutineRunContext` (`run_types.py`) has a `scope_context` field.
- When a routine fires, `routine_manager` populates it via `load_scope_for_source(kind="routine", source_id=routine.routine_id)` — which reads `configs/routines/public/<id>.scope.yaml` if present, else returns `None`.
- `ToolRoutineRunner` threads `run_ctx.scope_context` into the tool's `ToolMessage`.
- Pipeline/task/job runners ignore it (their payloads self-scope). `FunctionRoutineRunner` is not yet wired (functions self-scope; passing scope needs a signature change).

**Status:** inert until a routine actually ships a `<id>.scope.yaml` (none exist yet). The first intended consumer is `fetch_email` (see §10).

---

## 9. The courier path (secrets)

Some deterministic tools must use a secret no LLM is allowed to see (sending a password, refreshing an OAuth token, typing a credit-card number). The pattern:
- The tool calls `ScopeAdapter.for_courier_call(...)` to mint a **band-100** scope. This is deterministic code granting itself the capability to **unseal** — it is *not* an LLM acting.
- **Pods are pointers**, never the secret bytes: a pod carries an env-var name or a file path; the courier dereferences it at the deterministic boundary. The pod_id may be visible to agents; the body bytes never are.
- Credentials never enter LLM transcription, **both directions**: outbound secrets ride datapod refs; inbound tokens (JWTs etc.) ride `http_request`'s `seal_fields`. LLM transcription silently corrupts opaque base64.

Current courier callers: `execute_code`, `http_request`, `oauth_token_refresh` (×2), `web_type_secret`. This path is correct-by-design; leave it out of the loader. (Its only wart is the slightly misleading name.)

---

## 10. Authority bands

`approval.authority_level` (0..100). Conventions:
- **0** — toolless / "driving nothing." A bare extractor that calls no tools.
- **98** — compiled tasks.
- **99** — autonomous routines / subsystems **default**. They must act *without* a human approver (there's no human in the loop for a cron sweep), so the approval gate would deadlock at <99. Cap below 99 only as a *deliberate* restriction, never as "it's toolless today."
- **100** — courier / `.env`-deterministic admin **bypass only** (see §6, `>=100` bypass).

---

## 11. Recipes

### Add scope to a new pipeline
Create `app/assistant/pipelines/<id>/scope.yaml` (PERMISSION bucket only):
```yaml
approval:
  authority_level: 0
tools:
  allowed_tools: []            # fail-closed; list tools only if the pipeline calls them
resources:
  allowed_global_resources: [all]   # or the exact resource_* names the agents need
  resource_groups: [memory]
writes:
  write_unified_log: true
  write_kg: false
```
Build once at run start and thread it:
```python
from app.assistant.scope.loader import load_scope_for_source
scope = load_scope_for_source(kind="pipeline", source_id="<id>", actor_id="<id>_runner")
```

### Add scope to a cron tool routine
Create `configs/routines/public/<routine_id>.scope.yaml` (same shape). The `ToolRoutineRunner` will attach it automatically (§8). Omit the file = the tool runs scope-free (floor).

### A tool that runs an inner LLM
Do **not** hand-roll a `ScopeContext`. Read the caller's scope off the tool message and thread it into the inner agent's `Message`:
```python
caller_scope = getattr(tool_message, "scope_context", None)
inner_msg = Message(agent_input=..., scope_context=caller_scope)
result = agent.action_handler(inner_msg)
```
The tool may *narrow* `caller_scope` before passing it, but never widen it, and never substitute a fixed module-level constant.

### Invoke a manager from background/system code
Build a scope (system surface, named `actor_id` for provenance) and hand it on the `Message`; the manager's contract narrows further. Grant the **minimum** the target needs. Prefer the established `_mutation_scope()` pattern in `kg_investigator/finding_executor.py` for permissions the system default doesn't grant.

---

## 12. Anti-patterns (do NOT do these)

- **Hand-rolling `ScopeContext(...)`** in a consumer (tool/agent/manager). Consumers receive scope; they never build it. (A module-level `_FOO_SCOPE = ScopeContext(...)` constant is the classic smell.)
- **Giving a pipeline/task a routine-level scope.** They self-scope; a second scope is the exact divergence the unified model kills.
- **Authoring identity** (`owner_id`/`actor_id`/`room_id`) in a `scope.yaml`. Identity is stamped per request.
- **Using visibility (`always_show`/`hidden_tools`) as a permission lever.** `allowed_tools` is the only grant.
- **A tool inspecting `authority_level` to refuse work.** Tools *consume* scope as input (e.g. `send_email` reads `acting_as`); policy enforcement is `tool_caller`'s job, not the tool's.
- **Widening on the way down.** Scope only narrows. If you think a callee needs *more* than its caller, you have a courier-shaped problem (§9), not a scope-flow one.
- **Changing a `ScopeContext` field without notifying the user.** Scope is the locus of mode control; blast radius is system-wide.

---

## 13. Key files

| Path | Role |
|---|---|
| `app/assistant/utils/pydantic_classes.py` | `ScopeContext` + all sub-policies; `Message.scope_context`; `ToolMessage` |
| `app/assistant/scope/loader.py` | `load_scope` / `load_scope_for_source` — the single loader |
| `app/assistant/manager_runtime/services/scope_adapter.py` | ingress runtime: `apply` / resolve / derive / narrow / project; `for_sub_manager`; `for_courier_call`; `build_system_scope_context` |
| `app/assistant/manager_runtime/manager_invoker.py` | calls `ScopeAdapter.apply` before the manager runs |
| `app/assistant/manager_runtime/services/tool_scope_service.py` | downstream tool **visibility** filter |
| `app/assistant/lib/tool_execution/tool_access_control.py` | allow/block enforcement |
| `app/assistant/lib/tool_execution/tool_approval.py` | authority + approval gates (`>=100` admin bypass) |
| `app/assistant/room_session_manager/services/room_scope_builder.py` | room source builder (calls `load_scope` on the room's `scope.yaml`) |
| `app/assistant/routine_manager/` | cron: `routine_manager.py`, `run_types.py` (`RoutineRunContext`), `runners/*` |

---

## 14. Convergence status (2026-05-31)

The objective: **one loader, no ad-hoc `ScopeContext(` sprawl.** Where it stands:

- ✅ **On the loader (~63 sites):** all pipelines, subconscious, KG (investigator/resolution/ingest/maintenance), wiki_generator, task_runner, invoke_agent, belief_engine, EmiReminderHandler, dj_manager; rooms via `load_scope`.
- ✅ **Routine mechanism wired** (`a05a6e0a`) — inert until a routine ships a `scope.yaml`.
- 🟢 **Deliberate non-loader paths (correct, keep):** courier (`for_courier_call` ×5), `for_sub_manager`, the `_derive_system_scope` floor, and `ScopeContext.model_validate(...)` re-hydration sites (parsing an existing scope dict back to an object — not new policy).
- 🟡 **Parked legacy** (`build_system_scope_context` callers to retire): the `daily_summary`/MaintenanceManager cluster (×4) and `task_ir_actions` fallback; plus Orchestrator (future, intentionally untouched).
- 🔴 **Un-migrated tail** (still hand-roll `ScopeContext(`): `context_engine` (chat_scan, reasoning_agent), `location_manager`, `routes/me` & `routes/preferences`, `geoguessr`, `execution_trace/dojo`.
- 🔴 **First routine-scope consumer not done:** `fetch_email` needs a `scope.yaml` + the `email_parser` must thread `tool_message.scope_context` (kills the `_EMAIL_PARSER_SCOPE` constant in `email_utils.py`). Plus ~10 other hidden-LLM tools (scraper, web_parser, label, vision_*, ask_kg) to audit for the same thread-the-caller fix; and `FunctionRoutineRunner` to wire.

> When modifying scope: re-read the actual file first (this codebase's tooling has at times returned stale/garbled reads — verify anchors), prefer assert-before-write edits, and remember the blast radius is system-wide.
