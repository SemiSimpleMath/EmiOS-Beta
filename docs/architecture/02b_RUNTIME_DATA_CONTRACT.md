# Runtime Data Contract — the Blackboard keys the kernel owns

> Companion to [02_MANAGERS.md](02_MANAGERS.md) (the loop) and
> [01_AGENTS.md](01_AGENTS.md) (the agent shell). This doc is the registry of
> blackboard keys, conventions, and idioms the manager runtime itself reads and
> writes. These conventions are implemented across `MultiAgentManager`,
> `FlowController`, `ToolCaller`, `ToolResultHandler`, and the agent-runtime
> services — an agent output schema or a control node that collides with these
> names is steering the loop, whether it meant to or not.

## The trust model

`AgentResultApplier.apply_result_to_state` filters keys through
`_RESERVED_RUNTIME_KEYS`: a listed key is logged and skipped unless the
agent's configured `structured_output` explicitly declares it via Pydantic
`model_fields` or JSON-schema `properties`. Accepted keys follow
`global_output_keys` / `append_fields`. `AgentInputApplier` also skips listed
keys in a dict `agent_input`, with no schema exception; scope travels on
`Message.scope_context`.

The tables below describe runtime-owned conventions, not the exact filter
set. For example, `action`, `exit`, `error`, and `cancelled` are not in that
set. Trusted Python nodes and manager ingress `Message.data` write directly.
A schema declaration is permission to write a protected key, so review new
forms against the actual set in `agent_result_applier.py`.

## Reserved keys — loop control

| Key | Written by | Read by | Meaning |
|-----|-----------|---------|---------|
| `next_agent` | Delegator, control nodes, ingress seed, agent outputs | `_run_loop`, Delegator (early-return when set) | Explicit route override. Cleared by the standard agent input path (`AgentInputApplier.apply`); control nodes must clear/replace it explicitly. Delegator does not consume or clear it. |
| `last_agent` | `Agent._prepare_execution_context` (= agent name), FlowController (synthetic signal states), exit paths | Delegator `state_map[last_agent]` | Routing source state. Synthetic values are part of the vocabulary — see conventions below. |
| `exit` | `final_answer_node`, `manager_exit_node`, `graceful_exit_control_node` | `_run_loop` | Terminates the loop with reason "success". |
| `error` / `error_message` | any node/agent that hits a dead end; `request_handler` on exceptions | `_run_loop`, `handle_default_error_exit` | Terminates the loop with reason "error"; message surfaces in the abort report. |
| `cancelled` | `MAMInstanceManager.cancel`, `Orchestrator._request_cancel_instance` — **always `update_global_state_value`** (a top-scope write is discarded when a nested call scope pops) | `_run_loop` | Cooperative cancel; the loop returns an aborted ToolResult (`handle_exit_cancelled`). |
| `action` / `action_input` | planner-style agent outputs | FlowController, ToolCaller, switchboard-arguments nodes | The selected tool/agent/intrinsic and its raw input. |
| `tool_arguments` | tool-arguments/switchboard nodes | ToolCaller (`{target_name, arguments}`, target must equal `action`) | Normalized execution payload. |
| `calling_agent` | planner flow (before ToolCaller runs) | ToolCaller, ToolResultHandler | Whose history the tool result belongs to. |
| `result_writer` | FlowController when it stores a result | ManagerExitNode | Identifies an earlier result writer so a later terminal agent can supersede it. |
| `result` | FlowController (`done` / `return_control` payload), ToolResultHandler (last tool result envelope) | `final_answer_node`, `manager_exit_node`, ToolResultHandler agent-return path | The raw "winning" payload before final-answer normalization. Last-writer-wins. |
| `final_answer`, `final_answer_*` | `final_answer_node` / `graceful_exit_control_node` / agents with final_answer forms | `handle_exit`, response formatters | The normalized outbound envelope (see FinalAnswerNormalizer). |
| `manager_exit_kind` | `graceful_exit_control_node` (`"aborted"`) | `handle_exit` | Distinguishes an abort report from a completion. |

## Reserved keys — runtime plumbing

| Key | Purpose |
|-----|---------|
| `scope_context`, `scope_contract_enforced` | The scope wall. Set at `request_handler`; re-validated at every agent activation and at ToolCaller. Never write these from an agent. |
| `task`, `information`, `agent_input` | The request payload (`request_handler` seeds the first two; `AgentInputApplier` unpacks the third per activation). |
| `manager_name`, `manager_loop_count`, `manager_loop_number`, `manager_agent_cycles`, `manager_max_cycles`, `manager_aborted_cycles`, `manager_abort_reason`, `manager_agent_steps` | Loop counters (global scope; the abort pair is stashed before the graceful-exit loop re-enters `_run_loop`). |
| `manager_flow_config`, `manager_control_node_configs`, `role_bindings` | Config published for control nodes / role resolution. |
| `manager_route_trace` | Rolling (200-entry) route log for diagnostics. |
| `_invocation_id`, `_invocation_started_utc` | Set by ManagerInvoker; the id keys mailbox delivery. |
| `_runtime_injections` | Per-agent steering slots appended by MailboxDispatcher; entries contain `text`, `posted_at_utc`, and `from_who`. |
| `pipeline_state` (via `utils/pipeline_state.py`) | `pending_tool` / `last_tool_result_ref` / flags; use the helper functions, not raw key access. |
| `room_id`, `room_surface`, `room_context_id`, `inbound_reply_to`, `seeded_chat_messages_count` | Room/transport context surfaced by `request_handler`. |
| `playwright_latest_snapshot(_id/_summary)`, `playwright_modal_map` | Browser state cards (manager-local; the snapshot card is ALSO published to `DI.global_blackboard` for ref tools). |

## FlowController intrinsics (the action vocabulary)

Actions that are not tools/agents, handled in `FlowController.route`:

- **`done`** — stores `result` and stamps `result_writer` only when the
  output's `result` is truthy; otherwise it leaves an existing result intact. Sets
  `last_agent = <agent name>`, clears `next_agent`. The state_map routes from
  the agent's own name.
- **`return_control`** — stores `result`, sets
  **`last_agent = "<agent>_return_control"`** — a synthetic signal state. A
  manager whose agent emits `return_control` at root scope MUST define that
  key in its state_map (e.g. `"emi_team::planner_return_control":
  "emi_team::final_answer"`). The validator checks prefixes of supplied
  edges, but does not require this edge for every agent that can emit it.
- **`error`** — rejected with `ValueError`. A missing/falsey action does nothing.
- **other truthy actions** — recorded as the pending tool (`set_pending_tool`);
  the state_map routes the flow to the switchboard/arguments/ToolCaller
  chain. In a nested call scope, `done`/`return_control` defer routing to
  ToolResultHandler (which pops the scope and returns to the caller).

Other synthetic `last_agent` states in the vocabulary: `graceful_exit` /
`max_limit` / `error_exit` (seeded by the exit paths) and
`<agent>_execute_dag` (MultiToolAgent's DAG handoff).

`_validate_strict_routing_config` enforces at manager construction: every
state_map **value** names a configured agent, control node, or role binding;
every existing `*_return_control` **key** has a prefix in the same name set
(agents, nodes, role aliases and targets). It does not validate that each
role target is instantiated or inspect output forms for missing edges.
Keys are otherwise an open vocabulary on purpose.

## Input idioms (how data enters a manager)

1. **`Message.data` spray** — `request_handler` writes every `data` key onto
   the blackboard verbatim. This is the blessed channel for ingress: rooms
   enter their flow by seeding `next_agent` here; task specs deliver
   `task_allowed_tools` / `allowed_read_files` / etc. Consequence: the
   `Message.data` namespace IS the blackboard namespace — the reserved keys
   above apply to it too. This write follows the `task`/`information` seed,
   so data can replace those values; later explicit scope/room/config writes
   take precedence over their data seeds.
2. **`Message.agent_input`** — per-activation unpack (dict → accepted keys
   after reserved-key filtering; str → `agent_input`) by `AgentInputApplier`.
3. **Direct pre-invoke writes** (`manager.blackboard.update_state_value(...)`
   before `invoke`) — legacy seeding; lands in the global scope. Prefer
   idiom 1; it goes through the same one channel every other caller uses.

## Result idioms (how data leaves a manager)

- The manager's ToolResult is built by `handle_exit` from the normalized
  `final_answer` envelope (`FinalAnswerNormalizer.normalize`): the
  `final_answer_*` fields plus carry-through (`result_summary`,
  `pod_references`) and `final_answer_data_list` (lifted leftover keys,
  stringified; an explicit list is preserved). The ToolResult's top-level
  `data_list` is separately `[{}]` on `handle_exit`.
- The normalizer's envelope stringifies structure (leftover keys become
  `final_answer_data_list` strings) — callers needing the selected raw payload
  read **`data["final_answer_raw"]`** on the returned ToolResult: the exit
  nodes stash the pre-normalization payload (manager_exit_node additionally
  captures the terminal agent's output when no `result`/`final_answer_*`
  was routed — the form-driven-last-agent flow shape), and `handle_exit`
  attaches it on success exits. When populated final fields match the latest agent
  output, its full structured payload is retained, including sibling domain fields.
  Earlier mismatched agent results are not searched as a fallback. Custom flows must
  still keep their winning terminal payload and final fields consistent.
- Aborts, including the default error fallback, return `manager_aborted` with structured
  abort data; completions return `final_answer`.

## Scope representations

`scope_context` is the single scope representation downstream of manager
ingress. The old full-knob projection (11 flattened resource/entity/write/
delivery keys plus a post-resolution `scope_contract` copy) fed zero readers
and was retired 2026-07-08 (the scope audit's Step-5 knob retirement).

What `_project_scope_to_runtime_data` still writes is genuinely merged
content, not a copy of scope:

- `scope_contract_enforced` — the enforcement flag all three walls check.
- `task_allowed_tools` / `task_except_tools` / `visible_tools` — the
  EFFECTIVE tool policy (resolved scope ∩ task-spec restrictions), read by
  the Planner (prompt filtering), ToolCaller (execution gate), and
  tool_scope_service (visibility). Never write these from agents or nodes.

Before projection, manager tool policy is resolved by `_apply_manager_narrowing`:
parent `["all"]` or a parent grant naming this manager admits its own declared
tool surface; otherwise the lists intersect. Denials accumulate and
`scope.tools.per_manager` further restricts the result. Authority/writes
cannot be expanded by a manager contract.

`visible_tools` is a prompt shortlist, not execution authorization.
`ToolScopeService` filters it against scope allow/block and authority. Its
ranked path does not separately apply task-level allow/except lists, and the
optional narrower receives the saved list before scope filtering; a final
scope filter runs afterwards. ToolCaller still checks task restrictions at
execution. These gaps are recorded in
[the runtime findings](../design/bug_list_2026-09-18.md#shared-runtime-follow-up-2026-09-19).

`data["scope_contract"]` is an ingress-side INPUT seed only (room ingress
writes it; `ScopeAdapter._resolve_scope_context` reads it while resolving
the scope). It is not rewritten after resolution.
