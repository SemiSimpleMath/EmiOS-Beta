"""Room-session-state RMW is serialized (room-session-manager audit RSM1, 2026-07-09).

The plan/task/doc/geoguessr binding services and the /actas service mutate their
state in the global blackboard with an unlocked load->mutate->save, and
activate/deactivate/status write two or three keys separately. Under concurrent
inbound turns (different threads) that lost updates and exposed torn state. Every
mutator + multi-key read now runs under one shared re-entrant lock.

These tests prove (deterministically) that the representative mutators acquire
that lock, that a mutator which nests another doesn't deadlock, and that
concurrent activations don't lose updates.
"""
from __future__ import annotations

import threading

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.room_session_manager.services._session_state_lock import (
    ROOM_SESSION_STATE_LOCK,
)
from app.assistant.room_session_manager.services.actas_session_service import ActAsSessionService
from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
from app.assistant.room_session_manager.services.plan_session_service import PlanSessionService


def _assert_blocks_on_lock(call) -> None:
    """With the shared lock held by this thread, a decorated call on another
    thread must block until we release — proving it acquires the lock."""
    done = threading.Event()

    def worker():
        try:
            call()
        finally:
            done.set()

    ROOM_SESSION_STATE_LOCK.acquire()
    try:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # A different thread cannot enter a lock-guarded method while we hold it.
        assert not done.wait(0.5), "call did not acquire the session-state lock (completed while held)"
    finally:
        ROOM_SESSION_STATE_LOCK.release()
    assert done.wait(2.0), "call did not complete after the lock was released"


class TestMutatorsAcquireLock:

    def test_base_activate_acquires_lock(self):
        svc = PlanSessionService(blackboard=GlobalBlackBoard())
        _assert_blocks_on_lock(
            lambda: svc.activate_room_binding(
                room_id="r1", surface="ui", context_id="main", initiated_by="t"
            )
        )

    def test_base_get_active_acquires_lock(self):
        svc = PlanSessionService(blackboard=GlobalBlackBoard())
        _assert_blocks_on_lock(
            lambda: svc.get_active_room_binding(room_id="r1", surface="ui", context_id="main")
        )

    def test_actas_set_principal_acquires_lock(self):
        svc = ActAsSessionService(blackboard=GlobalBlackBoard())
        _assert_blocks_on_lock(
            lambda: svc.set_principal(
                room_id="r1", surface="ui", context_id="main", principal="self"
            )
        )

    def test_geo_mutator_acquires_lock(self):
        bb = GlobalBlackBoard()
        geo = GeoguessrSessionService(blackboard=bb)
        binding = geo.activate_room_binding(
            room_id="r1", surface="ui", context_id="main", initiated_by="t"
        )
        sid = str(binding["session_id"])
        _assert_blocks_on_lock(
            lambda: geo.reset_round(session_id=sid)
        )


class TestReentrancy:

    def test_start_or_resume_does_not_deadlock(self):
        # start_or_resume_ticket_session (locked) nests get_active_ticket_session
        # (locked) and activate_room_binding (locked). A plain Lock would
        # self-deadlock; the shared lock is re-entrant.
        svc = PlanSessionService(blackboard=GlobalBlackBoard())
        result = svc.start_or_resume_ticket_session(
            ticket_id="tkt1", room_id="r1", room_context_id="main", room_surface="ui"
        )
        assert result["status"] == "active"
        assert result["ticket_id"] == "tkt1"
        # Resuming the same ticket returns the same session (no second activation).
        again = svc.start_or_resume_ticket_session(
            ticket_id="tkt1", room_id="r1", room_context_id="main", room_surface="ui"
        )
        assert again["plan_session_id"] == result["plan_session_id"]


class TestConcurrentActivationsNoLostUpdates:

    def test_many_rooms_activate_concurrently(self):
        # Every distinct room's activation must survive — with the shared lock
        # the RMWs on the one shared sessions/index dict serialize, so none is
        # clobbered and no index entry is orphaned.
        bb = GlobalBlackBoard()
        svc = PlanSessionService(blackboard=bb)
        n = 40
        barrier = threading.Barrier(n)

        def activate(i: int):
            barrier.wait()  # line them all up to maximize contention
            svc.activate_room_binding(
                room_id=f"room{i}", surface="ui", context_id="main", initiated_by="t"
            )

        threads = [threading.Thread(target=activate, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        sessions = svc._load_sessions()
        index = svc._load_room_index()
        assert len(index) == n, f"lost room-index entries: {len(index)} of {n}"
        assert len(sessions) == n, f"lost sessions: {len(sessions)} of {n}"
        # Every index entry points at a real, active session (no torn writes).
        for room_key, sid in index.items():
            assert sid in sessions, f"index {room_key} -> {sid} missing from sessions"
            assert sessions[sid]["status"] == "active"
