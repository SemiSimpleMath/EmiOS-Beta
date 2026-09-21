# Live execution ownership, cancellation, and safe takeover

Status: proposed implementation plan, 2026-09-20. Addresses DF37 and the remaining
WO1 worker-write gap. This document changes no runtime behavior.

## Objective

At any moment, identify every executing manager, agent activation, and tool call;
its invoking parent; its work object and main task attempt (if any); and the helper
record under which its work is attributed. Use that same ownership to cancel an
attempt, prevent further work by a stale owner, and decide whether takeover is safe.

Use the existing standard invocation/agent/tool paths. This is deterministic runtime
infrastructure, not a new LLM agent or an additional model call. Prompt explanations
of runtime state belong in Jinja; Python prepares structured fields.

## What exists today

- `manager_runtime/mam_instance_manager.py` tracks manager invocations, display names,
  room, thread, and start time. Its get_agents method returns loaded agents, not
  proof that those agents are executing. Cancellation sets one manager's flag.
- `manager_runtime/manager_invoker.py` registers and unregisters managers around the
  standard request handler. This is the entry point to extend, not bypass.
- `agent_classes/Agent.py::action_handler` is a candidate central agent-activation
  boundary. Audit overrides/direct invocations before relying on complete coverage.
- `dayflow_orchestrator/work_session.py` separately tracks dispatch threads by main
  task, overwriting that entry for a newer epoch even if the old thread is alive.
- `work_objects/runtime.py::WorkContext` carries work/node identity but not the
  captured main dispatch epoch or a shared cancellation signal.
- The sweeper records failure after 80 minutes of subtree inactivity without asking
  the worker to stop. Pending-finalization recovery may judge that result while the
  old worker is alive. Final-result fences already exist; worker-write fences and
  nested-call cancellation do not.

## Required invariants

1. Every active execution has a unique invocation/activation/call ID. Loaded agent
   instances are separately identifiable and never presented as executing by default.
2. Work-owned execution has immutable `(work_id, main_node_id, dispatch_epoch)`.
   `attribution_node_id` may identify an internal checklist/helper record beneath
   that main task. Helpers remain provenance, never schedulable main tasks.
3. Child calls inherit main-attempt ownership and cancellation. They may change
   attribution only within that task's owned subtree. Runtime code derives ownership;
   LLM output, mutable display names and thread names cannot assign it.
4. Cancellation requested, cancellation observed, and execution exited are different
   states. Never report a thread dead simply because its graph node timed out.
5. A newer attempt never erases an older live invocation. No new side-effecting work
   from a revoked attempt is admitted. Already-started operations remain accounted for.
6. The graph remains authoritative for planning/results. The live registry describes
   execution; it is not a second graph or a replacement result/finalizer pipeline.
7. Non-work invocations explicitly have no work binding. Ordinary chat retains the
   standard path without extra LLM calls, graph reads per token, or synchronous status
   file writes on every agent/tool transition.

## Ownership and status contract

Extend existing records with:

| Field | Meaning |
|---|---|
| process instance ID | PID plus boot UUID; distinguishes restarts and reused PIDs |
| invocation ID / parent invocation ID / root invocation ID | Actual invocation tree |
| work ID / main node ID / dispatch epoch | Immutable dispatch-attempt owner, or explicitly absent |
| attribution node ID | Main task or its internal helper/checklist record |
| activation ID / agent name | Each actual agent call, including repeat calls to the same instance |
| tool call ID / tool name | Current tool operation and its owner |
| lifecycle state | Registered, running, cancelling, exited |
| activity | Agent call, tool call, child-manager wait, user wait, or idle between calls |
| entered-at / last-progress-at | Activity timing; a heartbeat alone is not progress |
| cancel reason / requested-at / observed-at | Cooperative cancellation lifecycle |
| completion/outcome certainty | Settled versus interrupted or externally uncertain |

Model activity as nested spans so a parent waiting for a child does not obscure the
child's currently running tool. All live entries remain queryable until real exit;
retain a bounded recent-completion history for diagnosis. Group by work attempt or
room, while preserving the existing manager display-name addressing APIs.

## Phase 1 — establish ownership and truthful registration

Affected: WorkContext, work_session, ManagerInvoker, MAMInstanceManager, ManagerInterface.

- Introduce a small immutable work-attempt binding and an execution-context carrier.
  Capture the main epoch when dispatch is claimed; never re-read and adopt a newer
  epoch on behalf of an older invocation.
- Register the root dispatch attempt before its thread can run. Link its standard
  manager invocation to that root; key sessions by attempt, not just work/node.
- Derive parent invocation IDs from execution context. Pass the binding explicitly
  into spawned threads/executors; contextvars do not automatically cross all boundaries.
- Preserve main ownership through `_run_on_given_node`, `_run_on_child_node`, and
  discharge_node; validate helper attribution is inside the owned subtree.
- Extend existing registry rows/status payloads without breaking current consumers.
  Migrate WorkSession's liveness lookups into the same registry/service; keep a thin
  compatibility facade rather than two independently authoritative registries.
- Ensure try/finally covers registration, scope preparation, execution, and failures.
  A parent exit must not remove independently running child entries.

Acceptance: nested manager chains, helpers, direct chat and simultaneous unrelated
work all have correct ownership; failures leak no entries; two epochs are visible
at once in a controlled test and cannot overwrite each other's registration.

## Phase 2 — expose real agent and tool activity

Affected: Agent.action_handler and overridden activation paths, MultiAgentManager,
ToolCaller, _tool_caller_util, MCP executor, manager invocation status consumers.

- Audit actual agent activation paths; instrument the common execution boundary,
  including direct finalizer/composer calls. Do not double-count Planner overrides.
- Wrap agent activations and tool calls in begin/end spans with try/finally. Track
  children independently and restore parent activity on return.
- Explicitly identify waiting for user, nested manager, and external tool; do not
  infer these from a lack of graph writes.
- Keep status updates in memory under short locks. Coalesce publication to the
  existing runtime-monitor/resource status surface, including process identity.
- Expose a read-only tree: work -> main task -> attempt -> managers -> current
  agent/tool, with helper attribution alongside. Show cancelling and uncertain
  calls clearly; explicitly label invocations that are not attached to work.
- Inspect multi-process hosts: each process reports its own live registry; aggregate
  with process identity. A stale status file is historical evidence, not liveness.

Acceptance: a blocked fake tool remains visible as a running tool while its parent
waits; loaded but unused agents never appear active; exceptions close spans; existing
runtime monitor and mention routing remain compatible.

## Phase 3 — cancellation propagation and execution admission

Affected: registry.cancel, invocation/activation boundaries, shared tool execution,
WorkContext and worker graph-write paths.

- Give the attempt a shared cancellation signal; manager-local cancellation also
  covers its descendants. Children registered after cancellation inherit the stopped
  state immediately. Timeout and work abandonment request attempt cancellation.
- Check cancellation before starting an agent or child manager, after LLM return
  before reconciling its output, and immediately before admitting each tool call.
  Recheck after approval waits before executing the approved operation.
- Make cancellation versus new-call admission atomic under a short runtime lock.
  Once admitted, a call is tracked as in flight; do not hold that lock or a SQLite
  transaction across model/network execution. Define the boundary honestly: a call
  admitted just before cancellation may still execute and must be reconciled.
- Use tool/provider cancellation only where explicitly supported. Never inject
  asynchronous exceptions into Python threads or kill the whole application.
- Propagate the main attempt fence to helper writes, including WorkPlanner reconcile,
  WorkGraphTools, ManagerInterface helper creation/results and other direct writers.
  Validate owner epoch, active work state and revocation inside the store transaction.
- Preserve already-committed useful provenance. Reject later writes from the revoked
  attempt without treating rejection as a new task failure or launching a retry.

Acceptance: cancelling a parent reaches existing/new descendants; a cancelled model
response cannot schedule another tool or reconcile helper writes; stale helper writes
are rejected atomically; an already-running fake tool remains visible until it exits.

## Phase 4 — safe timeout, abandonment and takeover

Affected: dispatch_sweeper, work_session recovery, store claim/finalization, dispatcher.

- Replace "timeout means worker gone" with "timeout requests cancellation". Preserve
  the timeout reason and normal result/finalizer route, with explicit execution state.
- Persist a minimal attempt/revocation and in-flight-operation record where needed
  for restart and multi-process safety. Do not persist every agent loop or adopt
  event sourcing for the entire graph. Specify and test the migration before wiring.
- Prevent a successor claim while the prior attempt has unquiesced execution or an
  unresolved external side effect. Apply this to replacement node IDs as well as a
  retry of the same node, so replanning cannot evade the barrier. Conservative initial
  implementation: block replacement execution within that work object until settled;
  retain concurrency for independent work objects.
- Keep cancellation state durable before accepting late worker writes. The finalizer
  may assess recorded evidence, but its retry verdict does not override the execution
  admission barrier. The finalizer remains the only failure-count incrementer.
- If a call cannot be cancelled, leave it visibly cancelling/blocked. Do not continually
  re-notify the user. Request intervention only when it is actually needed to resolve
  a consequential unknown outcome.
- On process restart, do not resurrect old threads from a status file. Classify owned
  invocations as interrupted; reconcile durable tickets and operation receipts through
  existing recovery. If an external action may have succeeded before the crash, inspect
  its outcome or use supported idempotency before retrying. No claim of exactly-once
  behavior for arbitrary external tools.
- Do not interpret an abandoned graph as proof its running actions stopped. Keep the
  old execution visible and report later observable outcomes without reopening work.

Acceptance: fake hung operation -> cancellation -> finalization -> attempted takeover
cannot produce overlapping external actions; confirmed exit permits the next claim;
uncertain external outcome blocks repetition across restart and new node IDs.

## Phase 5 — verification and rollout

Use isolated stores, fake blocking tools, barriers/events and deterministic synchronization;
never exercise real email/device actions in regression tests.

Required scenarios:

1. Normal main task with nested helpers: complete attribution, one finalizer result.
2. Cancel while waiting on an LLM: no tool invocation or reconciliation afterward.
3. Cancel during nested work: all descendants observe cancellation, no new child slips in.
4. Long-running tool: remains visible after timeout; replacement is held until settlement.
5. Old worker resumes after cancellation: no new calls/writes; its existing outcome remains
   available for reconciliation and cannot overwrite its successor.
6. Cancel/registration and cancel/tool-admission races: every admitted call is accounted for.
7. Abandonment while running: cancellation reaches descendants; no silent continued work.
8. Two epochs and two separate work objects: truthful tracking without cross-cancellation.
9. Restart before/after a side effect: unknown outcome is explicit and not blindly retried.
10. Existing user-ticket recovery, ordinary chat, agent errors, approval denial and cleanup.
11. Runtime status reports actual active agents versus loaded instances, no phantom liveness.
12. No added model calls; bounded registry/history and coalesced publication. Measure ordinary
    chat overhead against baseline, including lock contention under concurrent work.

Develop in this order, with focused tests at each phase. Observability can ship first;
do not enable automatic cancellation/takeover changes until phases 3 and 4 pass together.
Update AGENTS/CLAUDE, developer architecture docs, and DF37/WO1 status as each contract is
implemented. Preserve current uncommitted changes; committing/pushing and restarting the
live app remain separate user-authorized steps.

## Completion criteria

The runtime can identify and cancel a main attempt and all descendants; show what is
still executing; reject stale work; and prevent an unsafe replacement. A timeout alone
never makes a running worker disappear or grants permission to repeat its external action.


## Implementation record — 2026-09-20

Implemented locally through the standard runtime. The current contract, additive
tables, migration/recovery rules, status surfaces and conservative limitations are
documented in `docs/architecture/EXECUTION_OWNERSHIP.md`. Verification uses isolated
stores and fake tools, including the actual monitored executor context boundary.
No live app restart, external tool operation, commit or push is part of this change.
