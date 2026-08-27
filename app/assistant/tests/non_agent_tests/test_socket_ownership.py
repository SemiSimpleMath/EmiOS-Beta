"""The most recent window that TALKED owns the conversation (2026-08-26).

Two chat replies were composed, persisted, and then dropped as RoomNotBound
while the user's tab happily kept POSTing. Cause: the tab's binding had been
swept for idleness, and the heartbeat acked unconditionally — so the client was
told it was healthy forever and never re-registered. The liveness check proved
the transport was open, which was never the thing that broke.

Ownership rule these tests pin:
  - a keepalive NEVER transfers ownership (a stale background window must not
    steal the room back from the window the user is typing in);
  - talking or registering DOES transfer it;
  - a socket that lost the room learns WHY, once.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.services.socket_manager import RoomNotBound, SocketManager


def _mgr() -> SocketManager:
    return SocketManager()


class TestBindingState:

    def test_owner_other_and_unbound(self):
        m = _mgr()
        assert m.binding_state("master_room", "sock_a") == SocketManager.UNBOUND
        m.bind("master_room", "sock_a")
        assert m.binding_state("master_room", "sock_a") == SocketManager.OWNER
        assert m.binding_state("master_room", "sock_b") == SocketManager.OTHER

    def test_talking_window_takes_ownership(self):
        """A second window that talks becomes the owner; the first goes passive."""
        m = _mgr()
        m.bind("master_room", "sock_a")
        displaced = m.bind("master_room", "sock_b")
        assert displaced == "sock_a"
        assert m.binding_state("master_room", "sock_b") == SocketManager.OWNER
        assert m.binding_state("master_room", "sock_a") == SocketManager.OTHER

    def test_missing_room_or_socket_reads_unbound(self):
        m = _mgr()
        m.bind("master_room", "sock_a")
        assert m.binding_state("", "sock_a") == SocketManager.UNBOUND
        assert m.binding_state("master_room", "") == SocketManager.UNBOUND


class TestReleaseReason:

    def test_idle_sweep_records_reason(self):
        m = _mgr()
        m.bind("master_room", "sock_a")
        m.sweep_stale(max_age_seconds=-1)          # everything is stale
        assert m.binding_state("master_room", "sock_a") == SocketManager.UNBOUND
        assert m.take_release_reason("sock_a") == SocketManager.RELEASE_IDLE_TIMEOUT

    def test_displacement_records_reason(self):
        m = _mgr()
        m.bind("master_room", "sock_a")
        m.bind("master_room", "sock_b")
        assert m.take_release_reason("sock_a") == SocketManager.RELEASE_DISPLACED

    def test_reason_is_read_once(self):
        m = _mgr()
        m.bind("master_room", "sock_a")
        m.bind("master_room", "sock_b")
        assert m.take_release_reason("sock_a") == SocketManager.RELEASE_DISPLACED
        assert m.take_release_reason("sock_a") is None

    def test_no_reason_for_untouched_socket(self):
        assert _mgr().take_release_reason("never_seen") is None


class TestSweptSocketCannotSelfHeal:
    """The regression itself: after a sweep the room is free, and the evicted
    socket can only get it back by RE-REGISTERING (which replays what it
    missed) — a heartbeat alone must not resurrect the binding."""

    def test_heartbeat_does_not_rebind_a_swept_socket(self):
        m = _mgr()
        m.bind("master_room", "sock_a")
        m.sweep_stale(max_age_seconds=-1)
        m.record_heartbeat("sock_a")               # keepalive from the zombie
        assert m.binding_state("master_room", "sock_a") == SocketManager.UNBOUND
        try:
            m.resolve_socket("master_room")
            raise AssertionError("expected RoomNotBound")
        except RoomNotBound:
            pass

    def test_reregistering_reclaims_and_delivers(self):
        m = _mgr()
        m.bind("master_room", "sock_a")
        m.sweep_stale(max_age_seconds=-1)
        m.bind("master_room", "sock_a")            # what register_chat_client does
        assert m.resolve_socket("master_room") == "sock_a"
