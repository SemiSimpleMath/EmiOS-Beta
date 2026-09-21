# Execution ownership and cancellation

Implemented locally, 2026-09-20. See the original design in
`docs/design/work_execution_ownership_plan_2026-09-20.md` and DF37/WO1 in the bug ledger.

## What owns execution

`MAMInstanceManager.execution` exposes the process execution registry. The standard
`ManagerInvoker` registers an invocation span; Agent activation wrappers register
actual calls, including overrides and direct agent calls. An override calling
`super()` is one activation. Loaded agents remain a separate diagnostic list.
The two standard tool dispatch paths register tool spans after approval and before
execution. User-ticket waits and tool-approval waits are identified separately.

A work binding is immutable `(store, work_id, main_node_id, dispatch_epoch)`.
WorkSession captures it before launching its thread. WorkContext inherits that
binding when entering a helper and validates that the helper belongs beneath the
same main task. Its attribution node can change; its main owner/epoch cannot.
ManagerInterface and legacy discharge entry points establish the same ownership
when called without an existing work execution. Helper records remain provenance.

The registry retains parent/root IDs and all live spans until actual exit, plus
256 recent completions. Parent exit does not erase children. WorkSession's thread
table is a compatibility view owned by this registry; it cannot overwrite a live
older session. `start_monitored_thread` and `MonitoredThreadPoolExecutor.submit`
copy execution and WorkContext, reserving a background span before submission.
Cancelled/failed submissions clean up that reservation. New custom raw threads
must use these helpers; contextvars do not propagate through raw threading APIs.

## Cancellation and admission

`MAMInstanceManager.cancel(id)` cancels descendants. For a work-bound manager it
also revokes the whole main attempt durably, since its helpers share that owner.
The old global blackboard flag remains supported. Orchestrator child cancellation
uses this standard API when an invocation ID exists.

Checks occur before child manager/agent execution, after model return before
result application, and immediately before each tool, including after approval.
The runtime lock orders cancellation against tool admission. The SQLite write
transaction independently checks durable ownership/revocation when admitting a
call. No lock or transaction spans a model/network call. A call admitted just
before cancellation may execute; its receipt remains accounted for.

Worker WorkStore writes validate the captured main epoch, dispatched state,
active work object and durable revocation inside the graph write transaction.
Workers cannot create independent work objects or write into another work object.
Normal result recording and finalizer writes retain their existing fenced paths.
Only the finalizer increments failure counts.

## Durable records and takeover

WorkStore creates two additive, idempotent tables alongside its graph tables:

- `work_execution_attempts`: work/main/epoch, process boot ID, OS PID and process
  creation time, reserved/running/exited/interrupted state and revocation reason.
- `work_execution_calls`: admitted call ID and owner, tool name, in-flight/settled/
  unknown state and a bounded result/exception preview. Arguments are not copied.

Claiming inserts a reserved attempt in the graph transaction. Starting registers
the running owner before thread launch. Failed launches release reservations.
Actual root exit releases the owner only after registered background descendants
also exit. Receipts can settle after cancellation without rewriting graph outcomes.

Timeout still uses the existing inactivity threshold and ordinary result/finalizer
route. Its result transaction also revokes the attempt. Work/task abandonment
revokes execution. Neither operation asserts that a running Python thread died.
Claiming replacement work is held across the whole work object while a previous
attempt is reserved/running or any call is in-flight/unknown. Changing task IDs or
a finalizer's retry verdict cannot bypass this hold. Independent work objects are
unaffected. The dispatch gate treats the hold as a hold, not a new task failure.

At store startup and before claims, OS process identity is checked. A proven exited
or replaced process becomes interrupted; unfinished external calls become unknown.
Access-denied process checks preserve the barrier. Never infer liveness from PID
alone, a stale JSON file, or silence in logs. Older releases have no operation
receipts: this migration cannot reconstruct their historical external outcomes.

Normal tool return settles its receipt. A missing return, explicit `outcome_unknown`,
or MCP transport failure (`mcp_call_failed`) remains unknown. Other returned tool
errors retain their ordinary result/finalizer semantics; the runtime relies on their
reported outcome contract. An exception from a non-manager tool is
conservatively unknown, including tools whose read-only nature is not established.
Manager wrappers are not external actions; their nested tool calls carry receipts.
This classification can hold a read-only failure pending review. Do not automatically
clear unknown receipts merely because a process or attempt exited.

Existing ticket recovery uses the original ticket and matching epoch, skips another
live process's owner, and resolves only that ticket's wait receipt from its recorded
outcome. Recovery spans may read/record/finalize a revoked attempt; tool admission
still refuses it. Other unknown calls require provider lookup/idempotency or actual
operator evidence. `WorkStore.resolve_execution_call(call_id, evidence=...)` only
resolves unknown calls and requires evidence; it is not an automatic retry tool.

## Visibility and performance

`/api/runtime/concurrency` and `/debug/runtime/concurrency` expose actual nested
attempt/manager/agent/tool/background/wait spans, helper attribution, cancellation
state and unresolved durable receipts. Work portfolio prompts show the execution
hold; finalizers also see recent result receipts. Prompt prose is in shared Jinja.

Ordinary chat has no work binding, no execution-receipt SQLite writes, no graph
reads from these guards, and no additional model calls. Span changes stay in memory.
A single daemon coalesces process snapshots (at most once per second while active,
five seconds while idle). Other process snapshots are shown only while fresh and
the PID/creation time still match. The publisher's heartbeat is not task progress.
Existing manager display-name/status APIs remain available (status schema 3).

Cancellation is cooperative. There is no asynchronous Python thread exception,
process kill, or exactly-once guarantee for arbitrary external services. Unsupported
blocking operations remain visibly cancelling until they return. The conservative
work-level barrier favors avoiding duplicate actions over starting replacement work.

## Verification

`app/assistant/tests/dayflow/test_execution_ownership.py` covers blocked fake tools,
uncertain outcomes across store connections, queued descendants, cancellation after
approval/model return, stale helper writes and epochs, abandonment, restart before/
after calls, additive migration, plain chat and prompt projections. Existing dispatch,
ticket recovery, result/finalizer and manager cancellation tests are retained.
Use isolated databases and fake tools; never test this by sending real messages or
operating devices. Restart, commit and push remain separate deployment steps.
