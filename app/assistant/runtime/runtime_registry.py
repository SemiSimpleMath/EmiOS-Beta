from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_thread_name(thread: threading.Thread | None) -> str:
    if thread is None:
        return ""
    return str(getattr(thread, "name", "") or "")


class RuntimeRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, dict[str, Any]] = {}
        self._executors: dict[str, dict[str, Any]] = {}

    def register_thread(
        self,
        *,
        thread_id: str,
        owner: str,
        kind: str,
        daemon: bool,
        metadata: dict[str, Any] | None = None,
        thread_ref: threading.Thread | None = None,
    ) -> None:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id is required.")
        with self._lock:
            self._threads[thread_id] = {
                "thread_id": thread_id,
                "owner": str(owner or "").strip() or "unknown_owner",
                "kind": str(kind or "").strip() or "thread",
                "daemon": bool(daemon),
                "metadata": dict(metadata or {}),
                "registered_at_utc": _now_utc(),
                "started_at_utc": None,
                "stopped_at_utc": None,
                "status": "registered",
                "thread_ref": thread_ref,
                "thread_name": _safe_thread_name(thread_ref),
                "thread_ident": getattr(thread_ref, "ident", None) if thread_ref is not None else None,
                "last_heartbeat_utc": None,
                "heartbeat_count": 0,
                "last_heartbeat_source": "none",
                "last_error": None,
            }

    def mark_thread_started(self, *, thread_id: str, thread_ref: threading.Thread | None = None) -> None:
        with self._lock:
            row = self._threads.get(thread_id)
            if row is None:
                raise ValueError(f"Unknown thread_id: {thread_id}")
            row["started_at_utc"] = _now_utc()
            row["last_heartbeat_utc"] = row["started_at_utc"]
            row["heartbeat_count"] = 1
            row["last_heartbeat_source"] = "startup"
            row["status"] = "running"
            if thread_ref is not None:
                row["thread_ref"] = thread_ref
            ref = row.get("thread_ref")
            row["thread_name"] = _safe_thread_name(ref)
            row["thread_ident"] = getattr(ref, "ident", None) if ref is not None else None

    def mark_thread_heartbeat(self, *, thread_id: str) -> None:
        with self._lock:
            row = self._threads.get(thread_id)
            if row is None:
                return
            row["last_heartbeat_utc"] = _now_utc()
            row["heartbeat_count"] = int(row.get("heartbeat_count", 0) or 0) + 1
            row["last_heartbeat_source"] = "runtime"

    def mark_thread_stopped(self, *, thread_id: str, error: str | None = None) -> None:
        with self._lock:
            row = self._threads.get(thread_id)
            if row is None:
                return
            row["stopped_at_utc"] = _now_utc()
            row["status"] = "error" if isinstance(error, str) and error.strip() else "stopped"
            row["last_error"] = str(error).strip() if isinstance(error, str) and error.strip() else None

    def register_executor(
        self,
        *,
        executor_id: str,
        owner: str,
        max_workers: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(executor_id, str) or not executor_id.strip():
            raise ValueError("executor_id is required.")
        workers = int(max_workers)
        if workers <= 0:
            raise ValueError("max_workers must be > 0")
        with self._lock:
            self._executors[executor_id] = {
                "executor_id": executor_id,
                "owner": str(owner or "").strip() or "unknown_owner",
                "max_workers": workers,
                "metadata": dict(metadata or {}),
                "registered_at_utc": _now_utc(),
                "shutdown_at_utc": None,
                "submitted": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "tasks": {},
            }

    def executor_submit(self, *, executor_id: str, task_name: str) -> str:
        with self._lock:
            row = self._executors.get(executor_id)
            if row is None:
                raise ValueError(f"Unknown executor_id: {executor_id}")
            task_id = f"{executor_id}:task:{uuid.uuid4().hex[:10]}"
            row["submitted"] += 1
            row["tasks"][task_id] = {
                "task_id": task_id,
                "task_name": str(task_name or "").strip() or "task",
                "submitted_at_utc": _now_utc(),
                "started_at_utc": None,
                "finished_at_utc": None,
                "status": "submitted",
                "last_error": None,
            }
            return task_id

    def executor_task_started(self, *, executor_id: str, task_id: str) -> None:
        with self._lock:
            row = self._executors.get(executor_id)
            if row is None:
                return
            task = row["tasks"].get(task_id)
            if not isinstance(task, dict):
                return
            task["started_at_utc"] = _now_utc()
            task["status"] = "running"
            row["running"] += 1

    def executor_task_finished(self, *, executor_id: str, task_id: str, error: str | None = None) -> None:
        with self._lock:
            row = self._executors.get(executor_id)
            if row is None:
                return
            task = row["tasks"].get(task_id)
            if not isinstance(task, dict):
                return
            task["finished_at_utc"] = _now_utc()
            task["status"] = "error" if isinstance(error, str) and error.strip() else "completed"
            task["last_error"] = str(error).strip() if isinstance(error, str) and error.strip() else None
            row["running"] = max(0, int(row.get("running", 0)) - 1)
            if task["status"] == "completed":
                row["completed"] += 1
            else:
                row["failed"] += 1

    def executor_shutdown(self, *, executor_id: str) -> None:
        with self._lock:
            row = self._executors.get(executor_id)
            if row is None:
                return
            row["shutdown_at_utc"] = _now_utc()

    def snapshot(self) -> dict[str, Any]:
        now_utc = _now_utc()
        with self._lock:
            threads_rows = []
            registered_identifiers: set[int] = set()
            for row in self._threads.values():
                thread_ref = row.get("thread_ref")
                is_alive = bool(thread_ref.is_alive()) if isinstance(thread_ref, threading.Thread) else False
                thread_ident = getattr(thread_ref, "ident", None) if isinstance(thread_ref, threading.Thread) else row.get("thread_ident")
                if isinstance(thread_ident, int):
                    registered_identifiers.add(thread_ident)
                started = row.get("started_at_utc")
                running_for_s = None
                if isinstance(started, datetime):
                    running_for_s = round((now_utc - started).total_seconds(), 2)
                threads_rows.append(
                    {
                        "thread_id": row.get("thread_id"),
                        "owner": row.get("owner"),
                        "kind": row.get("kind"),
                        "daemon": bool(row.get("daemon", False)),
                        "thread_name": _safe_thread_name(thread_ref) or str(row.get("thread_name") or ""),
                        "thread_ident": thread_ident,
                        "status": row.get("status"),
                        "is_alive": is_alive,
                        "registered_at_utc": row["registered_at_utc"].isoformat()
                        if isinstance(row.get("registered_at_utc"), datetime)
                        else None,
                        "started_at_utc": started.isoformat() if isinstance(started, datetime) else None,
                        "stopped_at_utc": row["stopped_at_utc"].isoformat()
                        if isinstance(row.get("stopped_at_utc"), datetime)
                        else None,
                        "last_heartbeat_utc": row["last_heartbeat_utc"].isoformat()
                        if isinstance(row.get("last_heartbeat_utc"), datetime)
                        else None,
                        "heartbeat_count": int(row.get("heartbeat_count", 0) or 0),
                        "last_heartbeat_source": str(row.get("last_heartbeat_source") or "none"),
                        "has_runtime_heartbeat": int(row.get("heartbeat_count", 0) or 0) > 1,
                        "running_for_s": running_for_s,
                        "last_error": row.get("last_error"),
                        "metadata": dict(row.get("metadata") or {}),
                    }
                )

            executor_rows = []
            for row in self._executors.values():
                tasks = row.get("tasks") if isinstance(row.get("tasks"), dict) else {}
                task_rows = []
                for task in tasks.values():
                    if not isinstance(task, dict):
                        continue
                    task_rows.append(
                        {
                            "task_id": task.get("task_id"),
                            "task_name": task.get("task_name"),
                            "status": task.get("status"),
                            "submitted_at_utc": task["submitted_at_utc"].isoformat()
                            if isinstance(task.get("submitted_at_utc"), datetime)
                            else None,
                            "started_at_utc": task["started_at_utc"].isoformat()
                            if isinstance(task.get("started_at_utc"), datetime)
                            else None,
                            "finished_at_utc": task["finished_at_utc"].isoformat()
                            if isinstance(task.get("finished_at_utc"), datetime)
                            else None,
                            "last_error": task.get("last_error"),
                        }
                    )
                # Keep payload bounded.
                task_rows = sorted(task_rows, key=lambda x: str(x.get("submitted_at_utc") or ""))[-200:]
                executor_rows.append(
                    {
                        "executor_id": row.get("executor_id"),
                        "owner": row.get("owner"),
                        "max_workers": int(row.get("max_workers", 0) or 0),
                        "registered_at_utc": row["registered_at_utc"].isoformat()
                        if isinstance(row.get("registered_at_utc"), datetime)
                        else None,
                        "shutdown_at_utc": row["shutdown_at_utc"].isoformat()
                        if isinstance(row.get("shutdown_at_utc"), datetime)
                        else None,
                        "submitted": int(row.get("submitted", 0) or 0),
                        "running": int(row.get("running", 0) or 0),
                        "completed": int(row.get("completed", 0) or 0),
                        "failed": int(row.get("failed", 0) or 0),
                        "saturation_ratio": (
                            round(int(row.get("running", 0) or 0) / float(int(row.get("max_workers", 1) or 1)), 4)
                            if int(row.get("max_workers", 0) or 0) > 0
                            else 0.0
                        ),
                        "metadata": dict(row.get("metadata") or {}),
                        "tasks": task_rows,
                    }
                )

        all_runtime_threads = list(threading.enumerate())
        unregistered = []
        for t in all_runtime_threads:
            ident = getattr(t, "ident", None)
            if isinstance(ident, int) and ident in registered_identifiers:
                continue
            unregistered.append(
                {
                    "thread_name": _safe_thread_name(t),
                    "thread_ident": ident,
                    "daemon": bool(getattr(t, "daemon", False)),
                    "is_alive": bool(t.is_alive()),
                }
            )

        return {
            "schema_version": 1,
            "generated_at_utc": now_utc.isoformat(),
            "threads": sorted(threads_rows, key=lambda x: str(x.get("started_at_utc") or "")),
            "executors": sorted(executor_rows, key=lambda x: str(x.get("executor_id") or "")),
            "python_runtime_threads": {
                "total": len(all_runtime_threads),
                "unregistered_count": len(unregistered),
                "unregistered_threads": unregistered,
            },
        }


_runtime_registry_instance: RuntimeRegistry | None = None
_runtime_registry_lock = threading.Lock()


def get_runtime_registry() -> RuntimeRegistry:
    global _runtime_registry_instance
    with _runtime_registry_lock:
        if _runtime_registry_instance is None:
            _runtime_registry_instance = RuntimeRegistry()
    return _runtime_registry_instance


def start_monitored_thread(
    *,
    owner: str,
    name: str,
    target: Callable[..., Any],
    daemon: bool,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    kind: str = "thread",
    metadata: dict[str, Any] | None = None,
) -> threading.Thread:
    if not callable(target):
        raise ValueError("target must be callable.")
    thread_args = tuple(args or ())
    thread_kwargs = dict(kwargs or {})
    reg = get_runtime_registry()
    thread_id = f"thread:{owner}:{name}:{uuid.uuid4().hex[:8]}"

    def _wrapped() -> None:
        reg.mark_thread_started(thread_id=thread_id, thread_ref=threading.current_thread())
        try:
            target(*thread_args, **thread_kwargs)
        except Exception as e:
            reg.mark_thread_stopped(thread_id=thread_id, error=str(e))
            logger.error("Monitored thread failed owner=%s name=%s error=%s", owner, name, e)
            logger.debug("Monitored thread exception details owner=%s name=%s", owner, name, exc_info=True)
            raise
        reg.mark_thread_stopped(thread_id=thread_id, error=None)

    thread = threading.Thread(target=_wrapped, daemon=daemon, name=name)
    reg.register_thread(
        thread_id=thread_id,
        owner=owner,
        kind=kind,
        daemon=daemon,
        metadata=metadata,
        thread_ref=thread,
    )
    thread.start()
    return thread


class MonitoredThreadPoolExecutor:
    def __init__(self, *, name: str, owner: str, max_workers: int, metadata: dict[str, Any] | None = None):
        workers = int(max_workers)
        if workers <= 0:
            raise ValueError("max_workers must be > 0.")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{name}-pool")
        self._registry = get_runtime_registry()
        self._executor_id = f"executor:{owner}:{name}:{uuid.uuid4().hex[:8]}"
        self._registry.register_executor(
            executor_id=self._executor_id,
            owner=owner,
            max_workers=workers,
            metadata=metadata,
        )

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        if not callable(fn):
            raise ValueError("submit fn must be callable.")
        task_name = getattr(fn, "__name__", None)
        if not isinstance(task_name, str) or not task_name.strip():
            task_name = "task"
        task_id = self._registry.executor_submit(executor_id=self._executor_id, task_name=task_name)

        def _wrapped() -> Any:
            self._registry.executor_task_started(executor_id=self._executor_id, task_id=task_id)
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                self._registry.executor_task_finished(
                    executor_id=self._executor_id,
                    task_id=task_id,
                    error=str(e),
                )
                raise
            self._registry.executor_task_finished(executor_id=self._executor_id, task_id=task_id, error=None)
            return result

        return self._executor.submit(_wrapped)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        self._registry.executor_shutdown(executor_id=self._executor_id)

    def __enter__(self) -> "MonitoredThreadPoolExecutor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.shutdown(wait=True)

