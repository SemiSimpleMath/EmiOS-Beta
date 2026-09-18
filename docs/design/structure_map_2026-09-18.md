# Structure map — Phase 0 (2026-09-18)

Derived from **reading the code**, not from existing docs. Where a doc and the code disagree below,
the code is cited and the doc is marked stale/false. Nothing here is cited from a file I did not
read end to end.

---

## 1. Reading log — files read END TO END this session

| Lines | File |
|------:|------|
| 830 | `app/assistant/manager_classes/MultiAgentManager.py` |
| 834 | `work_objects/store.py` |
| 546 | `app/assistant/agent_registry/agent_registry.py` |
| 506 | `app/assistant/dayflow_orchestrator/dayflow_scheduler.py` |
| 403 | `app/bootstrap.py` |
| 399 | `work_objects/model.py` |
| 173 | `app/assistant/multi_agents/dayflow_orchestrator_manager/config.yaml` |
| 171 | `docs/design/doc_improvement_task_2026-09-18.md` (the brief) |
| 157 | `app/assistant/agent_registry/agent_loader.py` |
| 139 | `app/assistant/control_nodes/control_node.py` |
| 121 | `app/assistant/multi_agents/dayflow_wake_manager/config.yaml` |
| 121 | `skills/extending-emi-managers/SKILL.md` |
| 91 | `app/assistant/multi_agents/dayflow_dispatch_manager/config.yaml` |
| 58 | `app/assistant/agent_classes/Delegator.py` |
| 24 | `app/assistant/manager_registry/manager_registry.py` |
| 200 | `CLAUDE.md` (supplied verbatim in session context) |
| **4,773** | **total** |

**Read in part only** (cited only for the lines I saw): `docs/INDEX.md` (first 40 of 99),
`EXTENDING.md` (first 45 of 88).

**Structural inventory taken** (directory listings + line counts, not reads): `docs/architecture/`
(38 files, 11,425 lines), `app/assistant/` (~40 packages), `app/assistant/multi_agents/` (35
managers), `app/assistant/rooms/` (8 rooms + contract + templates), `skills/` (31 tracked files).

### What I did NOT read — nothing below makes claims about these
Tool registry and the tool contract · `RoomSessionManager` and the transports · `SkillRegistry` ·
the individual dayflow control nodes (materializer, switchboard args, finalizer node, architect
node, the prep/persist pairs) · `work_session.py` · `dayflow_tick.py` · pipelines · KG · pods ·
subconscious · every architecture doc except the parts quoted below.

---

## 2. Structure map

### 2.1 Boot — `app/bootstrap.py :: initialize_services(app)`

Strictly ordered; consumers never run before their seed.

1. `_seed_config_templates()` — `configs/templates/*.template.json` → writable configs dir, first run only
2. `_seed_personal_resources()` — tracked `*.example` → live `.json`/`.md` (9 personal resources)
3. `_auto_detect_default_llm_provider()` — sets `DEFAULT_LLM_PROVIDER` when exactly one provider key exists
4. Then 27 `ServiceLocator.register(...)` calls in dependency order. The load-bearing sequence:
   `event_hub` → `db_manager` → `user_settings` → `afk_monitor` (started) → `agent_registry` →
   `tool_registry` (`load_tools`, `load_mcp_servers`, `load_mcp_tool_cache`, `load_installed_mcp_tools`)
   → `DI.agent_registry.load_agents()` → `agent_factory` → `manager_registry` → `multi_agent_manager_factory`
   → orchestrator trio → `global_blackboard` → `resource_manager` (+ providers `resource_accounts`,
   `resource_email_accounts`; lock `resource_user_email` → `acting_as: user`) → `skill_registry` +
   `skill_injector` → `reply_router` → `manager_invoker` → `agent_components_factory` →
   `room_session_manager` → `entity_catalog` → `env_registry` → `scheduler` → `ticket_manager`
5. Gated by `configs/subsystems.yaml` via `is_subsystem_enabled`: `background_tasks`, `dj_manager`
6. `atexit.register(shutdown_services)` — LIFO stop, SQLAlchemy engines disposed last

### 2.2 The one abstraction — `MultiAgentManager`

Every manager in the repo is this class; a manager is a **`config.yaml`**, not code. Rooms, the
dayflow tick, the wake pass and the dispatch room are all instances of it with different state maps.

**Construction** (`__init__`): fresh `Blackboard()` per instance → `AgentLoader.load_agents()` →
`_validate_strict_routing_config()` → `ToolScopeService()` → role bindings.

**`_validate_strict_routing_config()` — boot-time fail-fast, always on.** Raises `ValueError` on:
non-dict/empty `state_map`; non-string src/dst; `control_nodes` not a list or empty; **any state_map
VALUE that is not a configured agent, control node, or role binding**; a `*_return_control` key whose
prefix is not a configured agent; `flow_config.tool_return.tool_call_result_handler_node` missing from
`state_map`; a `critic` section missing `subject_agent`/`critic_agent`/`continue_agent`; a `summary`
section missing `source_agent`/`summary_agent`/`resume_agent`.
State-map **keys** are an open vocabulary (synthetic signals: `<agent>_return_control`,
`<agent>_execute_dag`, `graceful_exit`, `max_limit`, `error_exit`); **values** are closed.

**`request_handler(message)`** — the entry every invocation takes:
- `task`, `information` → blackboard
- **every key of `message.data` → blackboard** (this is the only bridge from trigger to nodes)
- `room_id` / `room_surface` / `room_context_id` from message attributes
- `scope_context` → blackboard + `scope_contract_enforced`; **absent scope RAISES in production**
  (`_apply_no_inbound_scope`), substituting an allow-all scope only under `EMI_TEST_MODE`/pytest
- `tool_scope_service.initialize_scope(...)`
- pipeline state reset; `manager_flow_config`; `manager_control_node_configs` (per-node `config:` blocks)
- global counters: `manager_name`, `manager_loop_count`, `manager_loop_number`, `manager_max_cycles`
- optional `ExecutionTraceRecorder` (`manager_config.execution_trace.enabled`)
- push root scope `root_scope_<uuid>`; hydrate `seeded_chat_messages`; `run_agent_loop()`
- `finally`: pop the root scope, unwinding any leftover non-root frames with a warning

**`_run_loop`** — per cycle: counters → `_drain_mailbox()` (`MailboxDispatcher`) → `cancelled`? →
budget checks → `exit`? → `error`? → delegator → resolve role binding → `get_agent_instance` →
**activate with `Message(data_type='agent_activation', scope_context=…, metadata=…)` and nothing else.**
- `max_cycles` budgets **LLM-agent activations only**; control nodes are free. Backstop
  `iteration_cap = max(max_cycles * 8, 40)` catches control-node routing loops.
- `route_source` is `explicit_override` when `next_agent` was already set, else `state_map`;
  every hop is logged and appended to `manager_route_trace` (last 200).

> **Consequence, and the single most load-bearing fact for control-node authors:** a control node
> can only see trigger data on the **blackboard**. `message.data` is empty on activation. A node
> that reads `message.data` silently gets nothing — which is exactly how every dayflow time-wake
> ran a full planning tick for two days (fixed 2026-09-18; see 2.5).

**Exit** — `handle_exit_reason` maps success → `handle_exit` (`ToolResult(result_type="final_answer")`,
or `"manager_aborted"` when `manager_exit_kind == "aborted"`); `cancelled` → immediate
`manager_aborted`; `max_cycles`/`error`/unknown → `handle_graceful_exit`, which stashes
`manager_aborted_cycles` + `manager_abort_reason`, then re-enters `_run_loop` with
`max_exit_cycles` (default 10) from the `max_limit` / `graceful_exit` / `error_exit` state.

### 2.3 Discovery — the real extension contracts

**Agents** — `AgentRegistry._load_all_agent_configs`, scanning `app/assistant/agents/`:
- any directory containing `config.yaml` is an agent; a `.ignore` file in it skips it
- **canonical name = `namespace::name`**, namespace derived from the directory path relative to
  `agents/` (multi-level dirs join with `::`); a `name:` already containing `::` is used verbatim
- `prompts/system.j2` and `prompts/user.j2` are **both mandatory** — a missing one raises
  `FileNotFoundError` at boot. `prompts/description.j2` is optional.
- structured output: `agent_form.py` **wins over** `config.yaml: structured_output`; inside it a class
  named exactly `AgentForm` is preferred, else the first `BaseModel` subclass (with a warning)
- optional `input_schema.py` → the agent's input model
- `class_name:` must name a file `app/assistant/agent_classes/<class_name>.py` containing a class of
  that exact name; missing either raises

**Control nodes** — `AgentRegistry._load_all_control_nodes`, scanning `app/assistant/control_nodes/*.py`:
- skips `control_node.py`, `__init__.py`, and **any `_`-prefixed file** (those are helper modules)
- the class must subclass `ControlNode` **and be defined in that module** (`__module__` check, so an
  imported base class is never picked up)
- the expected class name is the file stem CamelCased (`work_node_wake_prep_node` →
  `WorkNodeWakePrepNode`); if several local subclasses exist and none matches, boot raises
  "Ambiguous control node classes"
- **registry key = the filename stem**

**Managers** — `ManagerRegistry.preload_all`, scanning `app/assistant/multi_agents/*/`:
- a directory is a manager **iff it contains `config.yaml`**. Nothing else is required — the live
  `dayflow_wake_manager/` directory contains that one file and nothing more.

**Wiring agents/nodes into a manager** — `AgentLoader.load_agents`:
- iterates `config["agents"]` then `config["control_nodes"]`; **both keys are required**
- each entry's **`class:`** key (not `type:`) is CamelCase→snake_case'd and looked up in the registry;
  if it resolves to a `control_node`, that entry wins. This is the **manager-local alias** mechanism:
  `- name: return_control / class: ReturnControlNode` registers the instance under the local alias
  `return_control` while resolving the class from `return_control_node.py`.
- agents are constructed `(name, blackboard, agent_registry, tool_registry, llm_params, parent)`;
  control nodes `(name, blackboard, agent_registry, tool_registry)`

**Routing** — `Delegator.action_handler`: honours an already-set `next_agent` and returns early;
otherwise `state_map[last_agent]`, with the key `"NO_PREVIOUS_AGENT"` when `last_agent` is empty. A
missing transition sets `error` + "Delegator routing failed: missing state_map entry". The class
carries an explicit "do not modify without user permission" note and holds **no policy** — all
conditional policy lives in control nodes.

**Control-node config access** (`ControlNode`): `_flow_section_cfg(section)` reads
`manager_flow_config.<section>`; `_node_cfg()` reads `manager_control_node_configs[self.name]`;
`_merged_section_node_cfg(section)` overlays the two with the node's own config winning.
`_pop_and_route_to_calling_agent()` is the canonical return-to-caller (call context is a 3-tuple
`(calling_agent, called_agent, scope_id)`).

### 2.4 Work objects — the substrate (`work_objects/`)

**Design rules** (`model.py` docstring, verbatim intent): generic not type-per-table (`type`,
`status`, `relation` are OPEN strings — a new node type needs zero schema change); first-class
columns only for what the **engine** queries, everything else in `content` (NL) or `payload` (typed
bag); **ownership is a tree** on `parent_id` (≤1 parent, acyclic, never edges); **dependency is a DAG
of edges** in their own table, never JSON arrays; the append-only `events` table is the **source of
truth** and `nodes`/`edges` are a rebuildable projection; `ready`/`blocked` are **derived, never stored**.

**Tables** (`SCHEMA_SQL`): `work_objects`, `nodes`, `actions`, `edges`, `events`.

**Vocabularies** — known values, not constraints: node types `goal, plan, subtask, tool, notify(legacy),
evidence, artifact, question, verification`; edge relations `depends_on, produces, answers, verifies,
supersedes, supports, contradicts, references`; wake kinds `time, event, user_reply, signal`;
satisfied-when kinds `tool_success, all_owned_children_done, verified_by, user_signoff, quality_bar`;
side effects `read, mutate, irreversible`.

**Status families** (`FAMILY_BY_TYPE` → `TRANSITIONS`): `spine`, `notify` (legacy), `knowledge`,
`question`, `verification`. The spine lifecycle:
`proposed → actionable → dispatched → done → closed`, with `failed` reachable from all live states and
`failed → {dispatched, proposed, abandoned}` (re-open), `done → {closed, superseded, failed}`.
`closed` — not `done` — is the satisfied terminal (`_SATISFIED_STATUSES`), and `done → failed` exists
so a call that *returned* but achieved nothing can still reach the architect.

**Every write goes through `WorkStore.apply(op, data, actor)`** — 10 ops:
`create_work_object, add_node, add_edge, set_status, edit_node, set_work_status, attach_pod,
consume_finalizer_instruction, defer_node, record_action`.
Sequence: RLock + transaction → load → handler → `_rollup` → `validate()` → append event → persist.
Event and projection commit together or not at all.

**The fences in `_op_set_status`** — these are the system's actual safety rules:
1. **incarnation** — `expected_dispatch_epoch` mismatch refuses a zombie writer's stale outcome
2. **failed-node** — `actor == "architect"` on a `failed` node without `licensed` is refused
3. **transition legality** — checked **only when `target != node.status`** (a same-status write skips
   validation entirely)
4. **terminal obligations** for `closed`/`abandoned`/`superseded`: the *churn fence* (abandoning
   `actionable`, or `waiting` with a future wake, needs `licensed`), the *in-flight ask fence*
   (`dispatched` + `wake_kind=user_reply` cannot be written terminal), and a **mandatory non-empty
   `reason`**, recorded as `payload.terminal`
5. bookkeeping on the way through: `dispatch_epoch` bump entering `dispatched`; `failure_count` +
   `goal_unmet_attempts` (on the **goal**, which churn cannot launder) entering `failed`;
   `_cascade_abandon_subtree` on `{done, closed, abandoned, superseded}`; the `finalizer` payload
   (`verdict, outcome, recommendation, next_step, question_for_user, escalated, at`); `status_notes`

**`_rollup`** — an `abandoned` object never auto-completes. When the goal is satisfied it first checks
`_unconsumed_finalizer_instruction` (any node whose finalizer entry has a non-empty `next_step` and no
`consumed_at`) and **holds** if one exists, so an auto-completion cannot destroy a plan change the
architect has not read yet. Otherwise: status `done`, cascade-abandon startable nodes, then write the
goal's epitaph (after the cascade, so it is not overwritten).

**Concurrency**: one `sqlite3` connection shared across threads, **all** access serialized by an
`RLock` (reads included), WAL, `busy_timeout` 10s. Default path `work_objects/work.db`; dayflow passes
`emi.db`.

### 2.5 Dayflow — three managers, two entry paths, one gate

| Manager | Opened by | State map | Can it plan? |
|---|---|---|---|
| `dayflow_orchestrator_manager` | `DayflowScheduler._execute_tick` → `dayflow_orchestrator_cadence_tick` | intake_triage → context_enricher → strategic_planner_wo (steward) → **work_architect_node** → state_mover → materializer → action_selector → switchboard → **work_node_dispatch_node** → post_room_finalize | yes — this is the planning tick |
| `dayflow_wake_manager` | `DayflowScheduler._fire_work_node` | work_node_wake_prep → state_mover → state_mover_persist → work_node_wake_router → switchboard → work_node_dispatch → post_room_finalize | **no** — no intake, steward or architect exists in its state map |
| `dayflow_dispatch_manager` | `work_session.open_session`, one thread per claimed node | switchboard_arguments → dayflow_tool_caller → **work_finalizer_node** → final_answer | no — it executes one already-chosen call |

**`DayflowScheduler`** — constants `DEBOUNCE=60s`, `MIN_GAP=120s`, `POKE_MIN_INTERVAL=600s`,
`MAX_CEILING=1800s`, `STARTUP_TICK_DELAY=45s`, work-wake cap 200, failure-notify threshold 3.
- Two locks: `_lock` (flags, held for microseconds) and **`_run_gate` (held for an entire manager
  pass — by ticks AND wakes alike)**. That is the mutual exclusion; `_running` is only a flag.
- Subscribed events: `repo_update` (only `email`/`calendar`/`todo_task`/`scheduler_events`),
  `afk_state_changed` (only on `active`), `dayflow_ticket_responded`, `dayflow_work_progress`.
- `_execute_tick` gates on `setup_complete()` **and a non-empty `UnifiedLog2026`** (no chat history →
  no tick), and on a DB failure still arms a ceiling tick so the heartbeat cannot go dark.
- `finally` always arms the ceiling tick **then** `_arm_work_node_wakes()`, then any follow-up/queued poke.
- `_arm_work_node_wakes` scans active work objects for nodes with `wake_kind == "time"`, status in
  `(proposed, waiting)` and a `wake_at`; sorts soonest-first; caps at 200; one APScheduler date job per
  node, id `dayflow_work_wake::<work_id>::<node_id>`, `misfire_grace_time=600`.
- `_fire_work_node` takes `_run_gate`, **re-checks `is_ready` inside the gate** (so a wake queued
  behind another pass sees that pass's writes), builds a room scope, and opens
  `dayflow_wake_manager` with `data = {trigger, wake_reason, triggered_work_node, day_of_week}`.

---

## 3. Claims table — verified against the code above

### `CLAUDE.md` → "Dayflow Orchestrator" section
| Claim | Checked against | Verdict | Evidence |
|---|---|---|---|
| Tick pipeline includes `work_finalizer` between the evaluator and the architect | `dayflow_orchestrator_manager/config.yaml` | **FALSE** | `work_finalizer` is in neither `agents:` nor `state_map`; `work_finalizer_node` is declared only in `dayflow_dispatch_manager` |
| Tick pipeline includes `work_repair` adjudicating failed nodes | same | **FALSE** | not in `agents:`, `control_nodes:` or `state_map`; config comment records its 2026-09-16 retirement |
| Order is evaluator → finalizer → architect → repair → state_mover | same | **FALSE** | actual: `…context_enricher_persist → strategic_planner_wo_prep → strategic_planner_wo → …persist → work_architect_node → state_mover_prep → …` |
| Replans prune queued/held nodes only when licensed by "a finalizer amend" | `store.py::_op_set_status` churn fence | **STALE** | fence is real; the vocabulary is not — no `amend` verdict exists; the stored verdict set is `verdict/outcome/recommendation/next_step/question_for_user/escalated` |
| `sweep_stuck_work_nodes` fails orphaned jobs "for work_repair" | `dayflow_orchestrator_manager/config.yaml` comments | **STALE** | failures are the finalizer's; the architect acts on its route |
| Asks: re-ask is "a repair decision, not a timer" | same | **STALE** | repair is retired; the timeout path ends at the finalizer |
| "The item dispatch lane is retired (a guard in `work_node_dispatch_node` closes strays loudly)" | same config | **TRUE** | consistent with the live state map and its comments |
| Managers live in `app/assistant/multi_agents/<name>/config.yaml`, one instance per invocation | `manager_registry.py`, `MultiAgentManagerFactory` | **TRUE** | `preload_all` requires `config.yaml`; factory creates per invocation |
| Routing is deterministic via the Delegator's `state_map` lookup | `Delegator.pick_next_agent` | **TRUE** | exactly `state_map.get(last_agent)` |

### `skills/extending-emi-managers/SKILL.md` — auto-loaded when an agent is asked to add a manager
| Claim | Checked against | Verdict | Evidence |
|---|---|---|---|
| The file to create is `manager_config.yaml` | `manager_registry.py::preload_all` | **FALSE** | discovery requires `config.yaml`; a manager built per this skill is never registered — and fails silently |
| `__init__.py` is required | live manager dirs | **FALSE** | `dayflow_wake_manager/` contains only `config.yaml` and runs |
| "`ManagerInvoker` discovers them on import" | `bootstrap.py`, `manager_registry.py` | **FALSE** | `ManagerRegistry` discovers by directory scan at boot; `ManagerInvoker` only invokes |
| `DI.manager_invoker.invoke(<name>, message)` | `dayflow_scheduler.py::_fire_work_node` | **FALSE** | real call is `create_manager(name)` first, then `invoke(manager_instance, msg)` |
| Top-level `allowed_tools` / `blocked_tools` / `hidden_tools` | `MultiAgentManagerFactory.create` | **FALSE** | reads `config["tools"]["allowed_tools"]` and `except_tools`; `blocked_tools` exists only under `scope_contract.tools` |
| `control_nodes:` entries take `type:` | `AgentLoader._resolve_entry_from_declared_class` | **FALSE** | the loader reads **`class:`**; every live config uses `class:` |
| `state_map` starts at an `init:` key | `Delegator.pick_next_agent`, live configs | **FALSE** | first hop is keyed by the delegator's name (`room::delegator`); the empty-key fallback is `NO_PREVIOUS_AGENT` |
| `summary:` section takes `agent:` | `_validate_strict_routing_config` | **FALSE** | requires `source_agent`, `summary_agent`, `resume_agent` — a config with `agent:` raises at boot |
| `scope_contract: type: narrow_only` + `permissions.can_mutate_kg` | live configs | **FALSE** | real shape is `scope_contract.tools.{allowed_tools, blocked_tools, requires_approval_tools}` |
| Out-of-the-box node `approval_node` | `app/assistant/control_nodes/` | **FALSE** | no `approval_node.py`; the other five named files exist |
| "`_check_manager_configs` will reject unknown agent names" | `validation/agent_validator.py::_check_manager_configs` | **TRUE** | It exists and does exactly this: raises `RuntimeError` on an unknown agent name, warns on an unreachable declared agent. **I first recorded this as "not found" by reading a background search's output file while it was still being written and treating an empty section as absence — the null-search error this repo's hook exists to prevent. Corrected in commit `2c864b63`.** |
| Manager `allowed_tools` is the outer gate, per-agent narrows | — | **UNVERIFIED** | plausible and matches `ManagerFactory.create`, but I have not read the tool registry or `ToolScopeService` |

### `EXTENDING.md`
| Claim | Checked against | Verdict | Evidence |
|---|---|---|---|
| Manager pattern is `.../multi_agents/<name>/manager_config.yaml` | `manager_registry.py` | **FALSE → FIXED** | corrected to `config.yaml` this session (authorized one-liner) |
| The `extending-emi-*` skills exist and ship | `skills/` + `git ls-files` | **TRUE** | 31 tracked files under `skills/`; all 10 `extending-emi-*/SKILL.md` present. (`.claude/` *is* gitignored, but it holds duplicates — the canonical copies ship.) |
| Agent pattern `agents/<ns>/<name>/{config.yaml, prompts/, agent_form.py?}` | `AgentRegistry` | **TRUE but incomplete** | correct as far as it goes; `prompts/system.j2` **and** `user.j2` are both mandatory, which the table does not say |
| Tool pattern `lib/tools/<name>/{tool_contract.json, <name>.py}` | — | **UNVERIFIED** | tool registry not read |

---

## 4. Open questions — for you, not for me to guess

1. **`skills/` is not in the brief's scope, but it is the most load-bearing documentation in the
   repo.** These files carry `auto_inject_when.task_keywords`, so an agent asked to "add a manager"
   loads `extending-emi-managers` automatically — and that file is wrong in at least ten specifics,
   starting with the filename. Docs nobody reads are stale; *this* is documentation that actively
   misdirects an agent mid-edit. Should `skills/extending-emi-*` come into scope (I would put it
   ahead of several architecture docs), or stay out?

2. **Three surfaces answer "how do I add X"** — `EXTENDING.md`, `skills/extending-emi-*`, and
   (per the brief) a planned "How to extend EmiOS" page. Which is authoritative? My recommendation:
   the skills are the executable source of truth, `EXTENDING.md` stays a one-screen index pointing
   at them, and the brief's Level-3 page is dropped rather than added as a third copy.

3. **`docs/architecture/archive/` does not exist.** The brief says never delete, always archive.
   Create it on first use?

4. **Scope reality check.** 38 architecture docs / 11,425 lines. Reading each end to end and
   verifying its claims against code is several sessions, not one. My proposal for this session:
   fix `CLAUDE.md` (text for your approval first, as agreed) + `05a` + `08` + `04` — the four whose
   subject matter I have now actually read the code for — and leave the rest with a verified list of
   what is suspect. Agreed, or would you rather I go wider and shallower?

5. ~~**`_check_manager_configs`**~~ — settled: it exists (see the claims table). My "not found" was a
   null-search error, corrected in `2c864b63`.

---

## 5. Session outcome (appended after the work shipped)

Read end to end beyond §1: `tool_registry.py`, `validation/agent_validator.py`,
`rooms/room_bootstrap.py`, `rooms/room_resource_loader.py`, `rooms/ROOM_CONTRACT.md`, a real
`scope.yaml`, `skill_registry/{skill_registry,parser,skill_injector}.py`,
`resource_manager/resource_manager.py`, `routine_manager/run_types.py`,
`routine_handlers/__init__.py`, `configs/routines.json`, and the seven skills corrected.
**Total ≈ 11,400 lines.**

Shipped: seven `extending-emi-*` skills, `ROOM_CONTRACT.md`, `EXTENDING.md`, and four architecture
docs (`CLAUDE.md` + `AGENTS.md`, `05a`, `08`, `04`), one commit each.

### Verification pass (asked why these were unverified — fair question)

The list below was a rationing decision, not a property of the code: each item needed one or two
files. All were then read (`scope/loader.py`, `room_scope_builder.py`, `tool_scope_service.py`,
`tool_access_control.py`, `discharge.py`, plus targeted checks). **Three of the seven turned out to
be claims I had already shipped, wrong** — see commit `2d024750`:

1. **ROOM.md vs `scope.yaml` is an overlay**, not "scope.yaml is what the permission system reads":
   the scope is built from ROOM.md first, then seven permission blocks are replaced wholesale when
   a `scope.yaml` exists. A room without one keeps ROOM.md's permissions.
2. **`domain` / `actions` / `selectors` are NOT informational** — they drive planner visibility
   (an exact `domain` match outweighs an exact tool-name match) and can short-circuit the LLM
   narrower. I had shipped the opposite in the tools skill.
3. **`discharge_node` neither claims the node nor uses `manager_invoker`** — it raises unless the
   node is already `dispatched`, and invokes via `ManagerInterface.invoke_on`. 08 said otherwise.

Also settled: `approval_min_authority` is enforced (`tool_execution/tool_approval.py`);
`min_authority` is enforced at both the visibility and execution gates, with MCP/contract-less tools
exempt from the fail-closed 99; `flow_config.flow.<mode>.source_agent` is read at room ingress by
`_resolve_mode_source_agent`; and 08's "dead words" hold for production code.

**Genuinely still unverified — one item:** the per-agent paragraphs in `05a` §3 (each agent's model
name and context-item list). Cheap but tedious — ~15 `config.yaml` reads — and nothing in this
session's work depends on them. Only the stage/wiring claims around them were checked.

### Superseded: the original "left standing" list

- **`flow_config.flow.normal.source_agent`** — present in all three dayflow configs; nothing I read
  consumes it (router nodes read `source_agent` from *named* flow sections). Omitted from the
  managers skill rather than documented as a guess. Likely room-mode ingress; needs
  `RoomSessionManager`.
- **`ROOM.md` vs `scope.yaml` precedence** — both carry authority level, retention/write flags,
  delivery, allowed resources and entity cards. `scope.yaml`'s header asserts permissions live
  there; I did not read `app/assistant/scope/loader.py`, so the docs describe the split and point at
  `SCOPE.md` instead of ruling.
- **`approval_min_authority` enforcement** — the registry parses and range-checks it; the actual
  approval gate lives in `tool_scope_service` / dispatch, unread.
- **"informational only" tool metadata** (`domain`, `risk_level`, `side_effects`, …) — the tools
  skill's claim that no runtime gate reads them is plausible but unverified.
- **`08_WORK_OBJECTS` §"Known dead words"** — that no code *writes* `verified`/`stale`/`answered`/
  `unanswerable`/`active`/`passed` would need a repo-wide search to confirm; left as the audit note
  it already was.
- **`08_WORK_OBJECTS` runtime section** (`discharge_node` / `drive_work` steps) — `discharge.py`
  unread, so that section is untouched.
- **`05a` §3 agent-by-agent paragraphs** — model names and per-agent context lists were not
  re-verified; only the stage/wiring claims were.
