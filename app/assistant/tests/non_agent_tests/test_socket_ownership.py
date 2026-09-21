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


def test_registering_new_tab_disconnects_old_tab_and_preserves_delivery():
    """Exercise real Socket.IO disconnect callbacks, not a mocked disconnect API."""
    from types import SimpleNamespace
    from flask import Flask
    from flask_socketio import SocketIO
    from app.socket_handlers import register_socket_handlers

    app = Flask(__name__)
    app.config['TESTING'] = True
    manager = SocketManager()
    app.DI = SimpleNamespace(socket_manager=manager)
    socketio = SocketIO(app, async_mode='threading')
    register_socket_handlers(socketio)
    old = socketio.test_client(app)
    new = socketio.test_client(app)
    try:
        old.emit('register_chat_client', {'room_id': 'master_room'})
        old_sid = manager.resolve_socket('master_room')
        new.emit('register_chat_client', {'room_id': 'master_room'})
        new_sid = manager.resolve_socket('master_room')
        assert new_sid != old_sid
        assert not old.is_connected()
        assert new.is_connected()
        assert manager.resolve_socket('master_room') == new_sid
        assert any(e['name'] == 'socket_hijacked' for e in old.queue)
        new.get_received()
        socketio.emit('delivery_probe', {'text': 'new owner only'}, to=new_sid)
        assert [e['name'] for e in new.get_received()] == ['delivery_probe']
        # Re-registering the current owner must not disconnect it.
        new.emit('register_chat_client', {'room_id': 'master_room'})
        assert new.is_connected() and manager.resolve_socket('master_room') == new_sid
    finally:
        for client in (old, new):
            if client.is_connected():
                client.disconnect()
    assert not manager.is_bound('master_room')
