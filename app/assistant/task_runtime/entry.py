"""Task-runner entry points.

`run_task_workobject` starts the active drive for a freshly-instantiated task work object.
`resume_task_workobject` re-drives from the durable state after a wake fires (a node's precise
time-wake, an event, or a completion signal) or after a restart — the state lives entirely in the
work store, so resume is just "drive again."

In production the scheduler / run_task tool supply the store (`get_dayflow_work_store()`) and the
run scope (a task-execution scope). They're passed in here so the runner has no hidden globals and
stays unit-testable (the Phase-1 scenario drives + parks + resumes + simulates restart with these
directly, before the live scheduler wiring lands in Phase 3).
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
    _require_scope(scope)
    return drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)


def resume_task_workobject(store, work_id: str, *, scope, scope_contract_enforced: bool = True) -> str:
    _require_scope(scope)
    return drive(store, work_id, scope=scope, scope_contract_enforced=scope_contract_enforced)
