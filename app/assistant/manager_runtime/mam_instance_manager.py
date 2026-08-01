"""MAMInstanceManager — single API for all MultiAgentManager runtime concerns.

Owns:
- the registry of currently-running manager invocations
- per-instance display-name assignment (the ``Webby`` / ``Webby_2`` aliases)
- scoped lookups by display name + room_id (used by @mention router)
- read-through to each manager's loaded agents (used for diagnostics)
- cooperative cancel of a running invocation

This is the canonical "what's running, who do I address, how do I stop one"
API. Other modules consult it via ``DI.mam_instance_manager``; nothing else
should reach into manager runtime state directly.

ManagerInvoker registers/unregisters here; everyone else only reads.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.routine_manager.utils import status_dir
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ManagerInstanceRecord:
    """One row in the registry — everything we know about a running manager.

    The display_name is the user-facing alias (``Webby`` or ``Webby_2``)
    assigned at register time and stable for the lifetime of the
    invocation. base_display_name is the constant from manager config
    (``Webby``) used as the namespace key for collision detection.
    """
    invocation_id: str
    manager_name: str             # canonical type, e.g. "web_manager"
    manager_instance_name: str    # with UUID suffix, e.g. "web_manager_55d613a4"
    base_display_name: str        # from manager config, e.g. "Webby"
    display_name: str             # actual user-facing alias, e.g. "Webby_2"
    room_id: Optional[str]        # collision/visibility namespace
    reply_to: Optional[Dict[str, Any]]
    request_id: Optional[str]
    started_at_utc: datetime
    thread_name: str
    thread_ident: int
    manager_instance: Any = None  # live manager object (for cancel / agent access)
    metadata: Dict[str, Any] = field(default_factory=dict)


class MAMInstanceManager:
    """The runtime API for MultiAgentManager instances."""

    def __init__(self, *, resource_manager: Any = None) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, ManagerInstanceRecord] = {}
        # Per-namespace ``(base_display_name, room_id)`` counter that
        # tracks the highest suffix issued in the CURRENT generation.
        # A generation begins when a namespace transitions from empty to
        # non-empty and ends when its last active record unregisters.
        # Within a generation, the counter is monotonic (no slot reuse
        # mid-flight: if Webby_2 finishes while Webby is still active,
        # the next register becomes Webby_3, not Webby_2 reborn). On
        # the empty→non-empty transition the counter resets to 1, so
        # the next instance becomes plain ``Webby`` again.
        self._namespace_counters: Dict[tuple, int] = {}
        self._resource_manager = resource_manager

    # ------------------------------------------------------------------
    # Registration (called only by ManagerInvoker)
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        manager_instance: Any,
        manager_instance_name: str,
        manager_name: str,
        base_display_name: str,
        request_id: Optional[str],
        room_id: Optional[str],
        reply_to: Optional[Dict[str, Any]],
    ) -> ManagerInstanceRecord:
        """Add an invocation to the registry, assign its display_name, return
        the record. Caller (ManagerInvoker) must call ``unregister`` when
        the invocation completes (typically in a try/finally around the
        manager.request_handler call).

        ``invocation_id`` is generated here so the registry owns it.
        """
        invocation_id = self._build_invocation_id(manager_instance_name, request_id)
        current = threading.current_thread()
        with self._lock:
            display_name = self._assign_display_name(
                base_display_name=base_display_name,
                room_id=room_id,
            )
            record = ManagerInstanceRecord(
                invocation_id=invocation_id,
                manager_name=manager_name,
                manager_instance_name=manager_instance_name,
                base_display_name=base_display_name,
                display_name=display_name,
                room_id=room_id,
                reply_to=dict(reply_to) if isinstance(reply_to, dict) else None,
                request_id=request_id,
                started_at_utc=datetime.now(timezone.utc),
                thread_name=str(current.name or ""),
                thread_ident=current.ident or 0,
                manager_instance=manager_instance,
            )
            self._records[invocation_id] = record
        self._publish_status_to_disk()
        logger.info(
            "[mam] registered invocation_id=%s display_name=%s base=%s room_id=%s",
            invocation_id, display_name, base_display_name, room_id,
        )
        return record

    def unregister(self, invocation_id: str) -> None:
        """Remove an invocation. No-op if unknown.

        When the last record in a namespace unregisters, drops the
        namespace's high-water counter so the next register in the same
        namespace starts fresh at plain base name.

        Also clears the invocation's mailbox queue — nobody will ever
        drain it again (queues are keyed by the globally-unique
        invocation_id), so a post that raced the manager's exit must not
        sit in memory until process restart.
        """
        with self._lock:
            removed = self._records.pop(invocation_id, None)
            if removed is not None:
                ns_key = (removed.base_display_name, removed.room_id)
                still_active = any(
                    r.base_display_name == removed.base_display_name
                    and r.room_id == removed.room_id
                    for r in self._records.values()
                )
                if not still_active:
                    self._namespace_counters.pop(ns_key, None)
        if removed is not None:
            logger.info(
                "[mam] unregistered invocation_id=%s display_name=%s",
                invocation_id, removed.display_name,
            )
            try:
                from app.assistant.ServiceLocator.service_locator import DI
                mailbox = getattr(DI, "mailbox", None)
                if mailbox is not None:
                    mailbox.clear(invocation_id)
            except Exception:
                logger.debug("[mam] mailbox clear failed at unregister", exc_info=True)
        self._publish_status_to_disk()

    # ------------------------------------------------------------------
    # Lookups (used by @mention router, narrators, status panels)
    # ------------------------------------------------------------------

    def list_active(self, *, room_id: Optional[str] = None) -> List[ManagerInstanceRecord]:
        """Return active records, sorted by start time (oldest first).

        ``room_id`` filter scopes to a single room's active set — used by
        the @mention router so a Slack user only sees Slack-room
        invocations.
        """
        with self._lock:
            rows = list(self._records.values())
        if room_id is not None:
            rows = [r for r in rows if r.room_id == room_id]
        rows.sort(key=lambda r: r.started_at_utc)
        return rows

    def find_by_invocation_id(self, invocation_id: str) -> Optional[ManagerInstanceRecord]:
        with self._lock:
            return self._records.get(invocation_id)

    def find_by_display_name(
        self, display_name: str, *, room_id: Optional[str] = None,
    ) -> Optional[ManagerInstanceRecord]:
        """Exact display_name match within the room scope — the literal name
        or its typeable mention key (``waffle_graph`` → ``Waffle (graph)``;
        the @mention syntax cannot carry spaces/punctuation).

        ``Webby`` and ``Webby_2`` are distinct names; this method does
        not do "any Webby" matching. For that use ``find_by_base_display_name``.
        """
        from app.assistant.chat_narrator.display_names import mention_key
        if not display_name:
            return None
        target = display_name.strip().lower()
        for r in self.list_active(room_id=room_id):
            if r.display_name.lower() == target or mention_key(r.display_name) == target:
                return r
        return None

    def find_by_base_display_name(
        self, base_display_name: str, *, room_id: Optional[str] = None,
    ) -> List[ManagerInstanceRecord]:
        """All active records whose base_display_name matches (e.g. all
        Webbys: ``Webby``, ``Webby_2``, ``Webby_3``) — by literal name,
        mention key, or mention head (bare ``@waffle`` collects
        ``Waffle (graph)`` too; the router's ambiguity handling sorts out
        multiples). Used by the @mention router when the user types a bare name.
        """
        from app.assistant.chat_narrator.display_names import mention_head, mention_key
        if not base_display_name:
            return []
        target = base_display_name.strip().lower()
        return [
            r for r in self.list_active(room_id=room_id)
            if r.base_display_name.lower() == target
            or mention_key(r.base_display_name) == target
            or mention_head(r.base_display_name) == target
        ]

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------

    def cancel(self, invocation_id: str) -> bool:
        """Cooperatively cancel a running invocation.

        Sets ``cancelled=True`` in the manager blackboard's GLOBAL scope —
        this write happens from another thread, and the top scope may be a
        nested agent-call scope that gets popped (taking a top-scope write
        with it). The global scope survives every pop and the loop's check
        reads down the stack, so it always sees the flag. The MAM loop
        checks at the top of every cycle and exits via
        ``handle_exit_cancelled`` (an aborted ToolResult). Cancellation is
        best-effort — if the manager is mid-LLM-call when this fires, it
        cancels at the next cycle boundary.

        Returns True if the cancel was issued, False if no such invocation.
        """
        record = self.find_by_invocation_id(invocation_id)
        if record is None:
            return False
        manager = record.manager_instance
        if manager is None:
            return False
        try:
            blackboard = getattr(manager, "blackboard", None)
            if blackboard is None:
                return False
            blackboard.update_global_state_value("cancelled", True)
            logger.info(
                "[mam] cancel issued for invocation_id=%s display_name=%s",
                invocation_id, record.display_name,
            )
            return True
        except Exception:
            logger.warning(
                "[mam] cancel failed for invocation_id=%s",
                invocation_id, exc_info=True,
            )
            return False

    def get_agents(self, invocation_id: str) -> List[Any]:
        """Read-through to a manager's currently-loaded agent instances.

        Useful for diagnostics ("which agents are loaded for this Webby")
        and for future operations that target a specific agent. Returns
        empty list if the invocation isn't found or the manager doesn't
        expose a queryable agent_loader.
        """
        record = self.find_by_invocation_id(invocation_id)
        if record is None or record.manager_instance is None:
            return []
        try:
            loader = getattr(record.manager_instance, "agent_loader", None)
            if loader is None:
                return []
            registry = getattr(loader, "agent_registry", None)
            if registry is None:
                return []
            agents_attr = getattr(registry, "agents", None)
            if isinstance(agents_attr, dict):
                return [a for a in agents_attr.values() if a is not None]
        except Exception:
            logger.debug("[mam] get_agents failed", exc_info=True)
        return []

    # ------------------------------------------------------------------
    # Status surface (replaces ManagerInvoker.get_invocation_status)
    # ------------------------------------------------------------------

    def get_status_payload(self) -> Dict[str, Any]:
        """Snapshot of all active invocations in a JSON-friendly shape.

        Same shape as the old ``manager_invoker.get_invocation_status``
        return so existing consumers (dispatch_sweeper, status panel)
        keep working without changes.
        """
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            rows = list(self._records.values())
        out_rows = []
        for r in rows:
            running_for_s = round((now_utc - r.started_at_utc).total_seconds(), 2)
            out_rows.append({
                "invocation_id": r.invocation_id,
                "manager_name": r.manager_name,
                "manager_instance_name": r.manager_instance_name,
                "base_display_name": r.base_display_name,
                "display_name": r.display_name,
                "room_id": r.room_id,
                "request_id": r.request_id,
                "thread_name": r.thread_name,
                "thread_ident": r.thread_ident,
                "started_at_utc": r.started_at_utc.isoformat(),
                "running_for_s": running_for_s,
            })
        return {
            "schema_version": 2,
            "component": "mam_instance_manager",
            "generated_at_utc": now_utc.isoformat(),
            "active_invocation_count": len(out_rows),
            "active_invocations": sorted(out_rows, key=lambda row: row["started_at_utc"]),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_invocation_id(manager_instance_name: str, request_id: Optional[str]) -> str:
        return f"{manager_instance_name}:{request_id or 'no_request_id'}:{uuid.uuid4().hex[:8]}"

    def _assign_display_name(
        self, *, base_display_name: str, room_id: Optional[str],
    ) -> str:
        """Compute the next display_name for a new invocation.

        Algorithm: per-namespace high-water counter, monotonic within a
        generation, reset only when the namespace empties.

          - First register in an empty namespace: counter = 1,
            name = ``base`` (plain).
          - Subsequent register in non-empty namespace:
            counter = (current counter) + 1, name = ``base_<counter>``.
          - Counter persists when individual records unregister; it
            resets only when the LAST record in the namespace
            unregisters (handled in ``unregister``).

        Concrete behavior:
          - Reg 1 (empty)              → ``Webby``       (counter=1)
          - Reg 2 (Webby alive)         → ``Webby_2``    (counter=2)
          - Reg 3 (Webby + Webby_2)     → ``Webby_3``    (counter=3)
          - Webby_2 dies, Webby alive
          - Reg 4 (Webby alive)         → ``Webby_4``    (counter=4)
            (Note: NOT Webby_3 — no slot reuse mid-flight; this avoids
             the confusion of "Webby_2 came back from the dead.")
          - All Webbys die, namespace empties, counter resets
          - Reg 5 (empty)              → ``Webby``       (counter=1)

        Caller MUST hold ``self._lock``.
        """
        if not base_display_name:
            return base_display_name
        ns_key = (base_display_name, room_id)
        namespace_active = any(
            r.base_display_name == base_display_name and r.room_id == room_id
            for r in self._records.values()
        )
        if not namespace_active:
            self._namespace_counters[ns_key] = 1
            return base_display_name
        counter = self._namespace_counters.get(ns_key, 1) + 1
        self._namespace_counters[ns_key] = counter
        return f"{base_display_name}_{counter}"

    def _publish_status_to_disk(self) -> None:
        """Mirror the in-memory state to the resource_manager_invocation_status
        file so the existing UI status panel keeps reading the same path.
        Best-effort; never breaks register/unregister.
        """
        try:
            payload = self.get_status_payload()
            path = status_dir() / "resource_manager_invocation_status.json"
            write_json_atomic(path, payload)
            resource_manager = self._resource_manager
            if resource_manager is None:
                from app.assistant.ServiceLocator.service_locator import DI
                resource_manager = getattr(DI, "resource_manager", None)
            if resource_manager:
                resource_manager.update_resource(
                    "resource_manager_invocation_status", payload, persist=False,
                )
        except Exception:
            logger.debug("[mam] status publish failed", exc_info=True)
