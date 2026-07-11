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

from app.assistant.utils.pydantic_classes import ScopeContext
from app.assistant.task_runtime.task_runner import drive


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


def start_task_run(template: dict, *, store=None, scope=None, scope_contract_enforced: bool = True) -> dict:
    """Instantiate a work-object TEMPLATE into the task store and drive it. Returns {work_id, status}.
    A park arms a wake on the base timing engine; resume via `resume_task_run(work_id)`."""
    from app.assistant.task_runtime.wo_builder import instantiate_template
    store, scope = _resolve_store_scope(store, scope, str((template or {}).get("task_id") or "task"))
    _require_scope(scope)
    work_id = instantiate_template(store, template)
    status = drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)
    _after_drive(store, work_id, status)
    return {"work_id": work_id, "status": status}


def resume_task_run(work_id: str, *, store=None, scope=None, scope_contract_enforced: bool = True) -> dict:
    """Resume a parked task run (a wake fired, or boot re-derivation) — re-drive from the durable store."""
    store, scope = _resolve_store_scope(store, scope, "task")
    _require_scope(scope)
    status = drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)
    _after_drive(store, work_id, status)
    return {"work_id": work_id, "status": status}
