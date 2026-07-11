"""Task-owned work store + task-execution scope.

Tasks are a SEPARATE consumer of the work-object substrate (work_objects/): they use their OWN
WorkStore over their OWN db (`task_work.db` in the data dir), never dayflow's `emi.db` store. No
dayflow, no shared store, no driver discriminator — full separation.
"""
from __future__ import annotations

import threading

from work_objects.store import WorkStore
from app.assistant.utils.path_utils import get_data_dir
from app.assistant.utils.pydantic_classes import ScopeApprovalPolicy, ScopeContext

_lock = threading.Lock()
_store: WorkStore | None = None


def get_task_work_store() -> WorkStore:
    """The process-wide task work store — a work_objects WorkStore over `data/task_work.db`.
    A distinct db from dayflow's `emi.db`; tasks and dayflow never share a store."""
    global _store
    with _lock:
        if _store is None:
            _store = WorkStore(path=str(get_data_dir() / "task_work.db"))
        return _store


def build_task_scope(task_id: str = "task", *, authority: int = 98) -> ScopeContext:
    """A task-execution scope — authority 98 (as the taskrunner uses), tools default to allowed_tools=['all'];
    the per-tool authority floor (min_authority) is the real gate the tool executor enforces."""
    return ScopeContext(
        scope_id=f"scope::task::{task_id}", owner_id="PRINCIPAL_USER", actor_id="task_runner",
        surface="routine", approval=ScopeApprovalPolicy(authority_level=authority),
    )
