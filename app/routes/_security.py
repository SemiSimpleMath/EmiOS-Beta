"""Local-only gate for dev / admin / debug routes.

A request is treated as local ONLY if it (a) arrives from a loopback address AND (b) carries no
proxy/tunnel forwarding headers. This second condition matters because a Cloudflare/ngrok/reverse-
proxy front-end terminates the public connection and re-issues the request to Flask from 127.0.0.1
— so ``remote_addr`` alone would mark a tunneled, internet-origin request as "local". The presence
of a forwarding header (``X-Forwarded-For``, ``CF-Connecting-IP``, ``CF-Ray``, …) means the request
originated outside this machine, so we refuse it. This closes the dev surface even when the app is
fronted by a tunnel.

This is the first, zero-config fence. The stronger follow-up is a shared admin token the local UI
carries and external callers cannot forge — wire that here too when ready.
"""
from functools import wraps

from flask import abort, request

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Headers a proxy / tunnel adds. ANY of them present => the request came from outside this host,
# even if the local socket is loopback. Cloudflare: CF-Connecting-IP / CF-Ray / True-Client-IP.
_PROXY_HEADERS = (
    "X-Forwarded-For",
    "X-Real-IP",
    "X-Forwarded-Host",
    "Forwarded",
    "CF-Connecting-IP",
    "CF-Ray",
    "True-Client-IP",
)


def _present_proxy_headers() -> list[str]:
    return [h for h in _PROXY_HEADERS if request.headers.get(h)]


def is_local_request() -> bool:
    """True only for a genuinely local, non-proxied/non-tunneled request."""
    if _present_proxy_headers():
        return False
    remote = (request.remote_addr or "").strip()
    return remote in _LOOPBACK


def reject_if_not_local() -> None:
    """Flask ``before_request`` hook for an all-dev blueprint — 403s any non-local request."""
    if not is_local_request():
        logger.warning(
            "Blocked non-local request to %s from remote=%s proxy_headers=%s",
            request.path, request.remote_addr, _present_proxy_headers(),
        )
        abort(403)


def local_only(fn):
    """Decorator for a single dangerous route — 403 unless the request is genuinely local."""
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not is_local_request():
            logger.warning(
                "Blocked non-local request to %s from remote=%s proxy_headers=%s",
                request.path, request.remote_addr, _present_proxy_headers(),
            )
            abort(403)
        return fn(*args, **kwargs)
    return _wrapped
