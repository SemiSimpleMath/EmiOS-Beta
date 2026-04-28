from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ReplyRoute:
    request_id: str
    reply_to: Dict[str, Any]
    created_at_ts: float


class ReplyRouter:
    """
    Thread-safe mapping of request_id -> reply_to routing info.

    This is the backbone for multi-transport replies:
    - web Socket.IO (room=sid)
    - Twilio SMS (to/from numbers)
    - future: Slack/Email/etc
    """

    def __init__(self, *, ttl_seconds: float = 24 * 3600):
        self._ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._routes: Dict[str, ReplyRoute] = {}

    def set_route(self, request_id: str, reply_to: Dict[str, Any]) -> None:
        if not request_id:
            return
        if not isinstance(reply_to, dict) or not reply_to.get("type"):
            return
        now = time.time()
        with self._lock:
            self._routes[str(request_id)] = ReplyRoute(
                request_id=str(request_id),
                reply_to=dict(reply_to),
                created_at_ts=now,
            )

    def get_route(self, request_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not request_id:
            return None
        now = time.time()
        rid = str(request_id)
        with self._lock:
            r = self._routes.get(rid)
            if not r:
                return None
            if (now - r.created_at_ts) > self._ttl_seconds:
                # expire
                self._routes.pop(rid, None)
                return None
            return dict(r.reply_to)

    def prune(self) -> int:
        """Best-effort TTL pruning; returns number removed."""
        now = time.time()
        removed = 0
        with self._lock:
            for rid in list(self._routes.keys()):
                r = self._routes.get(rid)
                if not r:
                    continue
                if (now - r.created_at_ts) > self._ttl_seconds:
                    self._routes.pop(rid, None)
                    removed += 1
        return removed

