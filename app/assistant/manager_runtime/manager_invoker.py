from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.assistant.manager_runtime.request_preprocessor import RequestPreprocessor
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.routine_manager.utils import status_dir, write_json_file
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolResult

logger = get_logger(__name__)


class ManagerInvoker:
    """
    Canonical manager invocation entrypoint.
    """

    def __init__(self, preprocessor: RequestPreprocessor | None = None, *, resource_manager: Any = None):
        self._resource_manager = resource_manager
        self.preprocessor = preprocessor or RequestPreprocessor(resource_manager=resource_manager)
        self.scope_adapter = ScopeAdapter()
        self._lock = threading.Lock()
        self._active_invocations: dict[str, dict[str, Any]] = {}

    def _build_invocation_status_payload(self) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            rows = []
            for invocation_id, item in self._active_invocations.items():
                started = item.get("started_at_utc")
                running_for_s = None
                if isinstance(started, datetime):
                    running_for_s = round((now_utc - started).total_seconds(), 2)
                rows.append(
                    {
                        "invocation_id": invocation_id,
                        "manager_name": item.get("manager_name"),
                        "request_id": item.get("request_id"),
                        "thread_name": item.get("thread_name"),
                        "thread_ident": item.get("thread_ident"),
                        "started_at_utc": started.isoformat() if isinstance(started, datetime) else None,
                        "running_for_s": running_for_s,
                    }
                )
        return {
            "schema_version": 1,
            "component": "manager_invoker",
            "generated_at_utc": now_utc.isoformat(),
            "active_invocation_count": len(rows),
            "active_invocations": sorted(rows, key=lambda row: str(row.get("started_at_utc") or "")),
        }

    def get_invocation_status(self) -> dict[str, Any]:
        return self._build_invocation_status_payload()

    def _publish_invocation_status(self) -> None:
        payload = self._build_invocation_status_payload()
        status_path = status_dir() / "resource_manager_invocation_status.json"
        write_json_file(status_path, payload)
        resource_manager = self._resource_manager
        if resource_manager is None:
            from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
            resource_manager = getattr(DI, "resource_manager", None)
        if resource_manager:
            resource_manager.update_resource("resource_manager_invocation_status", payload, persist=False)

    def _register_invocation(self, *, invocation_id: str, manager_name: str, request_id: str | None) -> None:
        current = threading.current_thread()
        with self._lock:
            self._active_invocations[invocation_id] = {
                "manager_name": manager_name,
                "request_id": request_id,
                "thread_name": str(current.name or ""),
                "thread_ident": current.ident,
                "started_at_utc": datetime.now(timezone.utc),
            }
        self._publish_invocation_status()

    def _unregister_invocation(self, *, invocation_id: str) -> None:
        with self._lock:
            self._active_invocations.pop(invocation_id, None)
        self._publish_invocation_status()

    def invoke(self, manager_instance, user_message: Message) -> ToolResult:
        manager_name = str(getattr(manager_instance, "name", "manager") or "manager")
        request_id = str(getattr(user_message, "request_id", None) or "").strip() or None
        invocation_id = f"{manager_name}:{request_id or 'no_request_id'}:{threading.get_ident()}"
        self._register_invocation(invocation_id=invocation_id, manager_name=manager_name, request_id=request_id)
        try:
            normalized_message, immediate = self.preprocessor.preprocess(manager_name, user_message)
            if isinstance(immediate, ToolResult):
                return immediate
            manager_cfg = getattr(manager_instance, "manager_config", None)
            scoped_message = self.scope_adapter.apply(
                manager_name=manager_name,
                manager_config=manager_cfg if isinstance(manager_cfg, dict) else {},
                message=normalized_message,
            )
            return manager_instance.request_handler(scoped_message)
        except Exception as e:
            logger.error("[%s] manager invocation failed: %s", manager_name, e)
            logger.debug("[%s] manager invocation exception details", manager_name, exc_info=True)
            raise
        finally:
            self._unregister_invocation(invocation_id=invocation_id)

