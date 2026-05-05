from __future__ import annotations

from typing import Any

from app.assistant.manager_runtime.request_preprocessor import RequestPreprocessor
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolResult

logger = get_logger(__name__)


class ManagerInvoker:
    """Canonical manager-invocation entry point.

    Responsibility: orchestrate ONE call into a manager instance —
    preprocess the inbound message, apply scope, hand off to the
    manager's request_handler, and return its result. Cross-cutting
    concerns (registry of running instances, display-name assignment,
    cancel/pause control, status panel) live in
    ``MAMInstanceManager`` and are accessed via ``DI.mam_instance_manager``.

    This class does NOT track running invocations or know about chat
    surfaces. It registers each call with the instance manager at the
    start and unregisters in a try/finally so the registry can never
    drift from reality even if a manager raises.
    """

    def __init__(
        self,
        preprocessor: RequestPreprocessor | None = None,
        *,
        resource_manager: Any = None,
    ) -> None:
        self._resource_manager = resource_manager
        self.preprocessor = preprocessor or RequestPreprocessor(
            resource_manager=resource_manager,
        )
        self.scope_adapter = ScopeAdapter()

    # ------------------------------------------------------------------
    # Back-compat shims for callers that still ask the invoker for state.
    # The actual data lives in MAMInstanceManager.
    # ------------------------------------------------------------------

    def get_invocation_status(self) -> dict[str, Any]:
        from app.assistant.ServiceLocator.service_locator import DI
        mam = getattr(DI, "mam_instance_manager", None)
        if mam is None:
            return {
                "schema_version": 2,
                "component": "mam_instance_manager",
                "active_invocation_count": 0,
                "active_invocations": [],
            }
        return mam.get_status_payload()

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def invoke(self, manager_instance, user_message: Message) -> ToolResult:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.manager_runtime.active_workers import (
            canonical_manager_name,
        )
        from app.assistant.chat_narrator.display_names import display_name_for

        manager_instance_name = str(getattr(manager_instance, "name", "manager") or "manager")
        canonical_name = canonical_manager_name(manager_instance_name)
        request_id = str(getattr(user_message, "request_id", None) or "").strip() or None
        reply_to = self._extract_reply_to(user_message)
        room_id = self._extract_room_id(user_message, reply_to)
        base_display_name = display_name_for(canonical_name)

        mam = DI.mam_instance_manager
        record = mam.register(
            manager_instance=manager_instance,
            manager_instance_name=manager_instance_name,
            manager_name=canonical_name,
            base_display_name=base_display_name,
            request_id=request_id,
            room_id=room_id,
            reply_to=reply_to,
        )

        # Stash invocation_id on the manager's blackboard so control nodes
        # within the manager (mailbox dispatcher, etc.) can identify
        # themselves to cross-invocation services.
        try:
            blackboard = getattr(manager_instance, "blackboard", None)
            if blackboard is not None:
                blackboard.update_state_value("_invocation_id", record.invocation_id)
        except Exception:
            logger.debug("Failed to stash invocation_id on blackboard", exc_info=True)

        self._publish_invocation_started_event(record)
        try:
            normalized_message, immediate = self.preprocessor.preprocess(
                canonical_name, user_message,
            )
            if isinstance(immediate, ToolResult):
                return immediate
            manager_cfg = getattr(manager_instance, "manager_config", None)
            scoped_message = self.scope_adapter.apply(
                manager_name=canonical_name,
                manager_config=manager_cfg if isinstance(manager_cfg, dict) else {},
                message=normalized_message,
            )
            return manager_instance.request_handler(scoped_message)
        except Exception as e:
            logger.error("[%s] manager invocation failed: %s", canonical_name, e)
            logger.debug(
                "[%s] manager invocation exception details",
                canonical_name, exc_info=True,
            )
            raise
        finally:
            mam.unregister(record.invocation_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _publish_invocation_started_event(record) -> None:
        """Fire a generic ``manager_invocation_started`` event on event_hub.

        ManagerInvoker doesn't know what consumes this — chat narrators,
        progress UIs, audit pipelines, etc. all subscribe at their own
        layer. Includes the per-instance display_name and reply_to so
        listeners can address and route without consulting any other
        state. Never raises into the host invocation.
        """
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            DI.event_hub.publish(
                Message(
                    sender="manager_invoker",
                    receiver=None,
                    event_topic="manager_invocation_started",
                    data={
                        "manager_name": record.manager_name,
                        "manager_instance_name": record.manager_instance_name,
                        "base_display_name": record.base_display_name,
                        "display_name": record.display_name,
                        "request_id": record.request_id,
                        "invocation_id": record.invocation_id,
                        "room_id": record.room_id,
                        "reply_to": record.reply_to,
                    },
                )
            )
        except Exception:
            logger.debug("manager_invocation_started publish failed", exc_info=True)

    @staticmethod
    def _extract_reply_to(user_message) -> dict | None:
        """Pull ``reply_to`` from the inbound message's ScopeContext.

        ScopeContext is the canonical home — propagated through chained
        sub-managers via ``manager_interface.execute``. Falls back to
        ``message.metadata['reply_to']`` for inbound paths that haven't
        adopted scope-based reply_to yet.
        """
        try:
            scope = getattr(user_message, "scope_context", None)
            if isinstance(scope, dict):
                rt = scope.get("reply_to")
                if isinstance(rt, dict) and rt:
                    return dict(rt)
            elif scope is not None:
                rt = getattr(scope, "reply_to", None)
                if isinstance(rt, dict) and rt:
                    return dict(rt)
            meta = getattr(user_message, "metadata", None)
            if isinstance(meta, dict):
                rt = meta.get("reply_to")
                if isinstance(rt, dict) and rt:
                    return dict(rt)
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_room_id(user_message, reply_to: dict | None) -> str | None:
        """Resolve the room_id this invocation is scoped to.

        Source priority:
          1. scope_context.room_id (canonical, propagates through chains)
          2. reply_to.room_id (when scope is absent)
          3. message.room_id top-level (set on direct inbounds)

        Returns None for invocations with no room context (cron / batch /
        test). Such invocations all share a global None namespace for
        display-name assignment.
        """
        try:
            scope = getattr(user_message, "scope_context", None)
            if isinstance(scope, dict):
                rid = scope.get("room_id")
                if isinstance(rid, str) and rid.strip():
                    return rid.strip()
            elif scope is not None:
                rid = getattr(scope, "room_id", None)
                if isinstance(rid, str) and rid.strip():
                    return rid.strip()
            if isinstance(reply_to, dict):
                rid = reply_to.get("room_id")
                if isinstance(rid, str) and rid.strip():
                    return rid.strip()
            rid = getattr(user_message, "room_id", None)
            if isinstance(rid, str) and rid.strip():
                return rid.strip()
        except Exception:
            return None
        return None
