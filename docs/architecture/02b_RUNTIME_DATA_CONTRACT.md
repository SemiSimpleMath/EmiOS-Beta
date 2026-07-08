# Runtime Data Contract — the Blackboard keys the kernel owns

> Companion to [02_MANAGERS.md](02_MANAGERS.md) (the loop) and
> [01_AGENTS.md](01_AGENTS.md) (the agent shell). This doc is the registry of
> blackboard keys, conventions, and idioms the manager runtime itself reads and
> writes. Everything here is enforced by code paths in `MultiAgentManager`,
> `FlowController`, `ToolCaller`, `ToolResultHandler`, and the agent-runtime
> services — an agent output schema or a control node that collides with these
> names is steering the loop, whether it meant to or not.

## The trust model in one paragraph

`AgentResultApplier.apply_result_to_state` writes **every key of an agent's
structured output verbatim** onto the blackboard (honoring
`global_output_keys` / `append_fields`). There is no reserved-key filter: the
agent's output schema (agent_form.py / `structured_output`) is the gate. That
is what makes the control plane work — `action`, `exit`, `next_agent` are
ordinary keys — and it is also why a new agent form must not name a key below
unless it MEANS to drive the loop.

## Reserved keys — loop control

| Key | Written by | Read by | Meaning |
|-----|-----------|---------|---------|
| `next_agent` | Delegator, control nodes, ingress seed, agent outputs | `_run_loop`, Delegator (early-return when set) | Explicit route override. Cleared at the START of every agent activation (`AgentInputApplier.apply`) and consumed fresh each cycle. |
| `last_agent` | `Agent._prepare_execution_context` (= agent name), FlowController (synthetic signal states), exit paths | Delegator `state_map[last_agent]` | Routing source state. Synthetic values are part of the vocabulary — see conventions below. |
| `exit` | `final_answer_node`, `manager_exit_node`, `graceful_exit_control_node` | `_run_loop` | Terminates the loop with reason "success". |
| `error` / `error_message` | any node/agent that hits a dead end; `request_handler` on exceptions | `_run_loop`, `handle_default_error_exit` | Terminates the loop with reason "error"; message surfaces in the abort report. |
| `cancelled` | `MAMInstanceManager.cancel`, `Orchestrator._request_cancel_instance` — **always `update_global_state_value`** (a top-scope write is discarded when a nested call scope pops) | `_run_loop` | Cooperative cancel; the loop returns an aborted ToolResult (`handle_exit_cancelled`). |
| `action` / `action_input` | planner-style agent outputs | FlowController, ToolCaller, switchboard-arguments nodes | The selected tool/agent/intrinsic and its raw input. |
| `tool_arguments` | tool-arguments/switchboard nodes | ToolCaller (`{target_name, arguments}`, target must equal `action`) | Normalized execution payload. |
| `calling_agent` | planner flow (before ToolCaller runs) | ToolCaller, ToolResultHandler | Whose history the tool result belongs to. |
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
| `_invocation_id` | Set by ManagerInvoker; keys the mailbox drain. |
| `_runtime_injections` | Per-agent steering slots appended by the MailboxDispatcher; each agent renders its own slot at prompt time. |
| `pipeline_state` (via `utils/pipeline_state.py`) | `pending_tool` / `last_tool_result_ref` / flags; use the helper functions, not raw key access. |
| `room_id`, `room_surface`, `room_context_id`, `inbound_reply_to`, `seeded_chat_messages_count` | Room/transport context surfaced by `request_handler`. |
| `playwright_latest_snapshot(_id/_summary)`, `playwright_modal_map` | Browser state cards (manager-local; the snapshot card is ALSO published to `DI.global_blackboard` for ref tools). |

## FlowController intrinsics (the action vocabulary)

Actions that are not tools/agents, handled in `FlowController.route`:

- **`done`** — stores `result` (payload or whole output), sets
  `last_agent = <agent name>`, clears `next_agent`. The state_map routes from
  the agent's own name.
- **`return_control`** — stores `result`, sets
  **`last_agent = "<agent>_return_control"`** — a synthetic signal state. A
  manager whose agent emits `return_control` at root scope MUST define that
  key in its state_map (e.g. `"emi_team::planner_return_control":
  "emi_team::final_answer"`). Validated at construction.
- **anything else** — recorded as the pending tool (`set_pending_tool`);
  the state_map routes the flow to the switchboard/arguments/ToolCaller
  chain. In a nested call scope, `done`/`return_control` defer routing to
  ToolResultHandler (which pops the scope and returns to the caller).

Other synthetic `last_agent` states in the vocabulary: `graceful_exit` /
`max_limit` / `error_exit` (seeded by the exit paths) and
`<agent>_execute_dag` (MultiToolAgent's DAG handoff).

`_validate_strict_routing_config` enforces at manager construction: every
state_map **value** names a configured agent, control node, or role binding;
every `*_return_control` **key** belongs to a configured agent. Keys are
otherwise an open vocabulary on purpose.

## Input idioms (how data enters a manager)

1. **`Message.data` spray** — `request_handler` writes every `data` key onto
   the blackboard verbatim. This is the blessed channel for ingress: rooms
   enter their flow by seeding `next_agent` here; task specs deliver
   `task_allowed_tools` / `allowed_read_files` / etc. Consequence: the
   `Message.data` namespace IS the blackboard namespace — the reserved keys
   above apply to it too.
2. **`Message.agent_input`** — per-activation unpack (dict → keys; str →
   `agent_input`) by `AgentInputApplier`.
3. **Direct pre-invoke writes** (`manager.blackboard.update_state_value(...)`
   before `invoke`) — legacy seeding; lands in the global scope. Prefer
   idiom 1; it goes through the same one channel every other caller uses.

## Result idioms (how data leaves a manager)

- The manager's ToolResult is built by `handle_exit` from the normalized
  `final_answer` envelope (`FinalAnswerNormalizer.normalize`): the
  `final_answer_*` fields plus carry-through (`result_summary`,
  `pod_references`) and `data_list` (lifted leftover keys, stringified).
- The normalizer's envelope stringifies structure (leftover keys become
  `data_list` strings) — callers needing an agent's raw STRUCTURED output
  read **`data["final_answer_raw"]`** on the returned ToolResult: the exit
  nodes stash the pre-normalization payload (manager_exit_node additionally
  captures the terminal agent's output when no `result`/`final_answer_*`
  was routed — the form-driven-last-agent flow shape), and `handle_exit`
  attaches it on success exits. Never scrape a finished manager's
  blackboard audit messages for output.
- Aborts return `result_type="manager_aborted"` (graceful-exit report or
  cancel); completions return `result_type="final_answer"`.

## Scope representations

`scope_context` is the single scope representation downstream of manager
ingress. The old full-knob projection (11 flattened resource/entity/write/
delivery keys plus a post-resolution `scope_contract` copy) fed zero readers
and was retired 2026-07-08 (the scope audit's Step-5 knob retirement).

What `_project_scope_to_runtime_data` still writes is genuinely merged
content, not a copy of scope:

- `scope_contract_enforced` — the enforcement flag all three walls check.
- `task_allowed_tools` / `task_except_tools` / `visible_tools` — the
  EFFECTIVE tool policy (scope ∩ task-spec ∩ room restrictions), read by
  the Planner (prompt filtering), ToolCaller (execution gate), and
  tool_scope_service (visibility). Never write these from agents or nodes.

`data["scope_contract"]` is an ingress-side INPUT seed only (room ingress
writes it; `ScopeAdapter._resolve_scope_context` reads it while resolving
the scope). It is not rewritten after resolution.
