"""Task-runner entry points.

`run_task_workobject` starts the active drive for a freshly-instantiated task work object.
`resume_task_workobject` re-drives from the durable state after a wake fires (a node's precise
time-wake, an event, or a completion signal) or after a restart — the state lives entirely in the
work store, so resume is just "drive again."

In production the run_task tool supplies a TASK-owned work store (a work_objects WorkStore over the
task store's db — NOT dayflow's store; tasks are a separate consumer of the substrate) and the run
scope (a task-execution scope). They're passed in here so the runner has no hidden globals and
stays unit-testable (the Phase-1 scenario drives + parks + resumes + simulates restart with these
directly). Wake/resume rides the base timing engine on a task-owned wake, independent of dayflow's
scheduler (Phase 3).
"""
from __future__ import annotations

import threading

from app.assistant.utils.pydantic_classes import ScopeContext
from app.assistant.task_runtime.task_runner import drive

# One drive at a time per work object: a wake-fired resume racing a manual run_task resume
# would otherwise both claim the same node (a same-status `dispatched` write passes the store's
# transition check silently) and execute its tools twice. In-process is the right scope — the
# store is single-process by design (verification finding R6/F8).
_RUN_LOCKS: dict[str, threading.Lock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def _run_lock(work_id: str) -> threading.Lock:
    with _RUN_LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(str(work_id), threading.Lock())


def _require_scope(scope) -> None:
    # Fail loud: a missing/invalid scope would make check_tool_access silently no-op (it only
    # enforces when scope_context is a ScopeContext) — i.e. the authority gate would vanish. A task
    # must always run under a real scope.
    if not isinstance(scope, ScopeContext):
        raise ValueError(f"task runner requires a ScopeContext, got {type(scope).__name__}")


def run_task_workobject(store, work_id: str, *, scope, scope_contract_enforced: bool = True) -> str:
    """Low-level: drive an already-instantiated work object with an explicit store + scope (used by tests)."""
    _require_scope(scope)
    return drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)


def resume_task_workobject(store, work_id: str, *, scope, scope_contract_enforced: bool = True) -> str:
    _require_scope(scope)
    return drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)


# --- high-level entry: instantiate a template into the TASK store, drive, arm wakes on parks ---

def _resolve_store_scope(store, scope, task_id: str):
    if store is None or scope is None:
        from app.assistant.task_runtime.task_store import build_task_scope, get_task_work_store
        store = store or get_task_work_store()
        scope = scope or build_task_scope(task_id)
    return store, scope


def _after_drive(store, work_id: str, status: str) -> None:
    if status == "parked":
        from app.assistant.task_runtime.task_scheduler import arm_task_wake
        arm_task_wake(store, work_id)   # base timing engine, not dayflow
    elif status in ("done", "failed", "stalled"):
        # terminal: this run's signal-router watches must stop consuming matches
        from app.assistant.task_runtime.task_events import cancel_task_watches
        cancel_task_watches(work_id)


def start_task_run(template: dict, *, store=None, scope=None, scope_contract_enforced: bool = True) -> dict:
    """Instantiate a work-object TEMPLATE into the task store and drive it. Returns {work_id, status}.
    A park arms a wake on the base timing engine; resume via `resume_task_run(work_id)`."""
    from app.assistant.task_runtime.wo_builder import instantiate_template
    store, scope = _resolve_store_scope(store, scope, str((template or {}).get("task_id") or "task"))
    _require_scope(scope)
    work_id = instantiate_template(store, template)
    # Register the run's event watches BEFORE the first drive — an early park must not race a
    # fast match. Raises if the template needs watches and the router is unavailable (a run
    # whose waits could never fire must not start).
    from app.assistant.task_runtime.task_events import register_task_watches
    register_task_watches(store, work_id)
    with _run_lock(work_id):
        status = drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)
        _after_drive(store, work_id, status)
    return {"work_id": work_id, "status": status}


def _record_event(store, work_id: str, event_name: str) -> None:
    """Record a fired event as an evidence node (data_id=event_observed); facts_context aggregates these
    into the `events_observed` list that wait-node guards and decision conditions read."""
    from work_objects.model import new_id
    goal = store.load(work_id).goal_node_id
    store.apply("add_node", {"work_id": work_id, "id": new_id("ev"), "type": "evidence", "parent_id": goal,
                             "status": "assumed", "title": "event", "content": str(event_name),
                             "payload": {"data_id": "event_observed", "value": str(event_name)}},
                actor="task_runner")


def _promote_event_waiters(store, work_id: str, event_name: str) -> None:
    """Wake-promotion: a delivered event moves each event-parked node subscribed to it from `waiting`
    to `actionable`, so it runs ONCE for this wake (and re-parks after, if it's a re-arming loop)."""
    wo = store.load(work_id)
    for n in list(wo.nodes.values()):
        if n.status != "waiting" or n.wake_kind not in ("event", "signal", "user_reply"):
            continue
        subs = [str(s) for s in (n.payload.get("subscriptions") or [])]
        if not subs or str(event_name) in subs:
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "actionable"},
                        actor="task_runner")


def resume_task_run(work_id: str, *, store=None, scope=None, observed_event: str | None = None,
                    scope_contract_enforced: bool = True) -> dict:
    """Resume a parked task run (a wake fired, or boot re-derivation) — re-drive from the durable store.
    `observed_event` (from signal_router or a clock fire) is recorded and promotes the subscribed
    event-parked node(s) so they run for this wake."""
    store, scope = _resolve_store_scope(store, scope, "task")
    _require_scope(scope)
    with _run_lock(work_id):
        if observed_event:
            _record_event(store, work_id, observed_event)
            _promote_event_waiters(store, work_id, observed_event)
        status = drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)
        _after_drive(store, work_id, status)
    return {"work_id": work_id, "status": status}
