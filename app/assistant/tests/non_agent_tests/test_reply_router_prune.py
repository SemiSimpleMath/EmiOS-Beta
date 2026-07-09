"""ReplyRouter self-wiring prune (delivery audit D4).

prune() existed but had zero callers — an expired route was only ever
forgotten if someone get_route'd that exact request_id after its TTL, so
the map leaked one entry per inbound turn indefinitely. set_route now
sweeps opportunistically, at most once per _PRUNE_INTERVAL_SECONDS.
"""
from __future__ import annotations

import time

from app.services.reply_router import ReplyRoute, ReplyRouter


def _backdate(router: ReplyRouter, request_id: str, age_seconds: float) -> None:
    old = router._routes[request_id]
    router._routes[request_id] = ReplyRoute(
        request_id=old.request_id,
        reply_to=old.reply_to,
        created_at_ts=old.created_at_ts - age_seconds,
    )


def test_set_route_prunes_expired_when_interval_elapsed():
    router = ReplyRouter(ttl_seconds=10)
    router.set_route("old-rid", {"type": "slack", "channel_id": "C1"})
    _backdate(router, "old-rid", age_seconds=100)

    router._last_prune_ts = time.time() - ReplyRouter._PRUNE_INTERVAL_SECONDS - 1
    router.set_route("new-rid", {"type": "socketio", "room_id": "master_room"})

    assert "old-rid" not in router._routes
    assert router.get_route("new-rid") == {"type": "socketio", "room_id": "master_room"}


def test_set_route_skips_prune_inside_interval():
    router = ReplyRouter(ttl_seconds=10)
    router.set_route("old-rid", {"type": "slack", "channel_id": "C1"})
    _backdate(router, "old-rid", age_seconds=100)

    # _last_prune_ts is fresh from __init__ -> no sweep on this set_route.
    router.set_route("new-rid", {"type": "socketio", "room_id": "master_room"})

    assert "old-rid" in router._routes           # sweep deferred (interval not elapsed)
    assert router.get_route("old-rid") is None   # but reads still honor the TTL
