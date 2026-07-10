"""Shared lock for room-session-state mutations on the global blackboard.

The plan / task / doc / geoguessr binding services and the /actas principal
service keep their state in the global blackboard (which is lock-free at the
value level — a single get or set is atomic, but a read-modify-write spanning
both is not). Every mutator here is a load -> mutate -> save, and
activate/deactivate/status write two or three keys (sessions + room index
[+ ticket index]) as separate sets. Concurrent inbound turns run on different
threads (Telegram/Slack workers, the UI daemon thread, the SMS request thread,
and — for geoguessr — a screenshot-timer thread), so without serialization those
RMWs lose updates and a reader can observe a torn state (a session written but
its index entry not yet) and conclude "no active mode".

One process-wide re-entrant lock guards the whole load->mutate->save span of
every mutator and every multi-key read, so each runs against a consistent
snapshot and the multi-key writes land together. Re-entrant because some
mutators call others (plan start_or_resume -> activate; deactivate ->
_on_deactivated). It is always acquired OUTSIDE the blackboard's own state lock
(the blackboard never calls back into these services), so the two can't deadlock.
"""
from __future__ import annotations

import functools
import threading

ROOM_SESSION_STATE_LOCK = threading.RLock()


def with_session_state_lock(method):
    """Serialize a session-state mutator/multi-key read under the shared lock."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        with ROOM_SESSION_STATE_LOCK:
            return method(*args, **kwargs)

    return wrapper
