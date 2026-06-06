"""Network + auth for the ID-anchored Bluesky tools (atproto / Bluesky XRPC).

Credential confinement (mirrors http_request's model):
  - The app password is resolved from its pod ONLY inside this module, at courier
    scope, used immediately to mint a session, and never stored or returned.
  - The session's access JWT lives in an in-process cache (``_SESSIONS``) keyed by
    handle — never the blackboard, transcript, or disk. Tools call get_timeline /
    create_record / download_image and never touch a credential.

Endpoint model: createSession hits the entryway (bsky.social); the response's
didDoc names the user's personal data server (PDS), and all subsequent XRPC calls
go there. Fail loud on every error — no silent degradation.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import requests

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_ENTRYWAY = "https://bsky.social"
_SESSION_TTL_S = 90 * 60  # atproto access tokens last ~2h; refresh comfortably before.
_TIMEOUT = 30.0

# In-process session cache keyed by handle. Holds the access JWT (a secret) — stays
# in memory only, never serialized anywhere an LLM or disk can observe it.
_SESSIONS: dict[str, dict] = {}


class BlueskyError(RuntimeError):
    """Any Bluesky/atproto failure. Tools convert this to a recoverable tool error."""


def _required_env(name: str) -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        raise BlueskyError(f"required env var {name!r} is unset/empty")
    return val


def _account() -> dict:
    from app.assistant.ServiceLocator.service_locator import DI

    acct = DI.env_registry.account_by_platform("bluesky")
    if not isinstance(acct, dict):
        raise BlueskyError(
            "no Bluesky account configured (env_registry.account_by_platform('bluesky') is empty)"
        )
    return acct


def _resolve_app_password(account: dict) -> str:
    """Courier-resolve the app-password pod to its bytes. The value is used for the
    immediate createSession call only — never cached, logged, or returned upward."""
    auth = account.get("auth") or {}
    pod_kind = str(auth.get("pod_kind") or "auth.bearer").strip()
    pod_id_env = str(auth.get("pod_id_env") or "").strip()
    if not pod_id_env:
        raise BlueskyError("Bluesky account entry has no auth.pod_id_env")
    pod_id = _required_env(pod_id_env)
    full_pod_id = f"datapod:{pod_kind}:{pod_id}"

    from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
    from app.assistant.pod_store.pod_store import PodStore

    courier_scope = ScopeAdapter.for_courier_call(tool_name="bluesky_client", request_id=None)
    value = PodStore().fetch_projection(full_pod_id, "full", scope=courier_scope)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    pw = str(value or "").strip()
    if not pw:
        raise BlueskyError(f"app-password pod {full_pod_id} resolved to an empty value")
    return pw


def _create_session(handle: str, app_password: str) -> dict:
    resp = requests.post(
        f"{_ENTRYWAY}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise BlueskyError(f"createSession failed: HTTP {resp.status_code} {resp.text[:200]!r}")
    data = resp.json()
    did = str(data.get("did") or "").strip()
    access = str(data.get("accessJwt") or "").strip()
    if not did or not access:
        raise BlueskyError("createSession response missing did/accessJwt")

    # Resolve the user's PDS from the did doc (subsequent calls go there, not the entryway).
    pds = _ENTRYWAY
    for svc in ((data.get("didDoc") or {}).get("service") or []):
        if isinstance(svc, dict) and svc.get("type") == "AtprotoPersonalDataServer":
            ep = str(svc.get("serviceEndpoint") or "").strip()
            if ep:
                pds = ep.rstrip("/")
                break

    return {"did": did, "access": access, "pds": pds, "handle": handle, "ts": time.time()}


def get_session(*, force: bool = False) -> dict:
    account = _account()
    handle = _required_env(str(account.get("handle_env") or "EMI_OWN_BLUESKY_HANDLE"))
    cached = _SESSIONS.get(handle)
    if cached and not force and (time.time() - cached["ts"]) < _SESSION_TTL_S:
        return cached
    sess = _create_session(handle, _resolve_app_password(account))
    _SESSIONS[handle] = sess
    logger.info("bluesky: minted session for @%s (pds=%s)", handle, sess["pds"])
    return sess


def _xrpc(method: str, nsid: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
    sess = get_session()
    url = f"{sess['pds']}/xrpc/{nsid}"
    resp = requests.request(
        method, url, headers={"Authorization": f"Bearer {sess['access']}"},
        params=params, json=json_body, timeout=_TIMEOUT,
    )
    if resp.status_code == 401:
        # Expired/invalid token — re-auth once and retry.
        sess = get_session(force=True)
        resp = requests.request(
            method, url, headers={"Authorization": f"Bearer {sess['access']}"},
            params=params, json=json_body, timeout=_TIMEOUT,
        )
    if resp.status_code != 200:
        raise BlueskyError(f"{nsid} failed: HTTP {resp.status_code} {resp.text[:200]!r}")
    try:
        return resp.json()
    except Exception as e:
        raise BlueskyError(f"{nsid} returned non-JSON response: {e}")


def get_timeline(limit: int = 20) -> dict:
    limit = max(1, min(int(limit or 20), 50))
    return _xrpc("GET", "app.bsky.feed.getTimeline", params={"limit": str(limit)})


def create_record(collection: str, record: dict) -> dict:
    sess = get_session()
    return _xrpc(
        "POST", "com.atproto.repo.createRecord",
        json_body={"repo": sess["did"], "collection": collection, "record": record},
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def download_image(url: str) -> str:
    """Download a post image to uploads/temp/bluesky_images/ and return its abs path.
    The planner-facing tool emits an ``[emi_image: <path>]`` marker pointing here so
    the prompt builder feeds the real pixels to a vision model."""
    from app.assistant.utils.path_utils import get_uploads_dir

    if not isinstance(url, str) or not url.startswith("http"):
        raise BlueskyError(f"invalid image url {url!r}")
    resp = requests.get(url, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise BlueskyError(f"image download failed: HTTP {resp.status_code}")
    ctype = (resp.headers.get("Content-Type") or "").lower()
    ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".gif" if "gif" in ctype else ".jpg"
    out_dir = get_uploads_dir() / "temp" / "bluesky_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"bsky_{uuid.uuid4().hex}{ext}"
    path.write_bytes(resp.content)
    return str(path)
