"""Single source of truth for pod ACCESS (scope + authority gating), the canonical
pod-id builder, and small shared helpers.

The authority *primitive* lives in ``authority.py`` (``check_authority`` /
``caller_authority`` / ``PodAuthorityError``); this module composes it with scope
resolution into the one gate every surface dereferences a pod through — the
``pod_fetch`` tool, the ``/pod expand`` slash command, and the ``/api/pods`` route.
Keeping the resolve-scope + check-authority logic here means no surface re-rolls it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.authority import (
    DEFAULT_POD_MIN_AUTHORITY,
    check_authority,  # noqa: F401  (re-exported for callers that want the primitive)
)
from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.pod_store.pod_uri import POD_URI_RE
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PodNotFound(Exception):
    """Pod is missing OR out of the caller's scope. Same response for both so callers
    never leak which non-readable pods exist."""

    def __init__(self, pod_id: str):
        self.pod_id = pod_id
        super().__init__(f"pod {pod_id} not found or out of scope")


def _slug(text: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return s or "x"


def canonical_pod_id(kind: str, *parts: Any) -> str:
    """Build a canonical pod id ``datapod:<snake_kind>:<12-hex token>`` where the token is a
    deterministic hash of ``parts`` (re-minting the same logical unit upserts ONE pod, and the
    id always matches ``pod_uri.POD_URI_RE`` so the PodInjector and the chat linkifier recognize
    it). Use this instead of hand-formatting pod ids — that is the bug the audit caught."""
    kind_slug = _slug(kind)
    raw = "|".join(str(p) for p in parts) if parts else kind_slug
    token = hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()
    pod_id = f"datapod:{kind_slug}:{token}"
    if not POD_URI_RE.fullmatch(pod_id):
        raise ValueError(f"canonical_pod_id produced a non-canonical id: {pod_id!r}")
    return pod_id


def current_room_scope_id(blackboard) -> Optional[str]:
    """The room a pod minted in this context belongs to: the originating ``room_id`` — stamped on
    the blackboard at every manager ingress and carried down the dispatch chain unchanged (identity,
    not permission). None for ownerless system/routine contexts (those pods become owner-only)."""
    try:
        rid = blackboard.get_state_value("room_id")
    except Exception:
        return None
    rid = str(rid or "").strip()
    return rid or None


def resolve_allowed_scopes(scope_ctx: Any) -> List[str]:
    """The concrete pod scope_ids a caller can read. Reads ``scope.pods.allowed_scopes`` (default
    ``["self"]``) and expands ``"self"`` -> the caller's room_id. Returns ``["all"]`` (unrestricted),
    a list of concrete scope_ids, or ``["__none__"]`` (no readable scope). A None scope_ctx returns
    ``["all"]`` (trusted system-internal callers — routines / dayflow ticks — that have no room).
    Accepts the scope as a ScopeContext object OR a model_dump dict."""
    if scope_ctx is None:
        return ["all"]
    if isinstance(scope_ctx, dict):
        pods_block = scope_ctx.get("pods") or {}
        raw = pods_block.get("allowed_scopes") if isinstance(pods_block, dict) else None
        room_id = scope_ctx.get("room_id")
    else:
        pods_obj = getattr(scope_ctx, "pods", None)
        raw = getattr(pods_obj, "allowed_scopes", None) if pods_obj is not None else None
        room_id = getattr(scope_ctx, "room_id", None)
    if not isinstance(raw, list) or not raw:
        raw = ["self"]
    if "all" in raw:
        return ["all"]
    out: List[str] = []
    for s in raw:
        s_str = str(s).strip()
        if not s_str:
            continue
        if s_str == "self":
            if isinstance(room_id, str) and room_id.strip() and room_id.strip() not in out:
                out.append(room_id.strip())
            continue
        if s_str not in out:
            out.append(s_str)
    return out or ["__none__"]


def pod_in_scope(pod_scope_id: Optional[str], allowed_scopes: List[str]) -> bool:
    """Scope wall: ``["all"]`` reads anything; otherwise the pod's ``scope_id`` must be one of the
    allowed concrete scopes (a None/empty pod scope_id is in no concrete set -> not readable)."""
    if allowed_scopes == ["all"]:
        return True
    if not pod_scope_id:
        return False
    return pod_scope_id in set(allowed_scopes)


def pod_min_authority(pod: Pod) -> int:
    """A pod's effective read floor — the per-pod override if set, else the DB default AUTH_CHAT(50)."""
    return int(pod.min_authority) if pod.min_authority is not None else DEFAULT_POD_MIN_AUTHORITY


def as_scope_object(scope: Any) -> Any:
    """Normalize a model_dump dict scope to a ScopeContext object so authority.caller_authority
    (which reads scope.approval.authority_level via getattr) works. None passes through."""
    if isinstance(scope, dict):
        try:
            from app.assistant.utils.pydantic_classes import ScopeContext
            return ScopeContext.model_validate(scope)
        except Exception as e:
            logger.warning("pod_utils: could not coerce scope dict to ScopeContext: %s", e)
    return scope


def read_pod_gated(pod_id: str, scope: Any) -> Dict[str, Any]:
    """THE pod read gate. Applies the scope wall then the authority wall, returns the body.

    Used by every surface that dereferences a single pod (pod_fetch siblings, /pod expand,
    /api/pods). Returns ``{pod_id, kind, one_liner, body, source_urls, scope_id}``.
    Raises ``PodNotFound`` (missing OR out of scope — indistinguishable on purpose) or
    ``PodAuthorityError`` (in scope but authority below the pod's floor). A None scope is a trusted
    system-internal read: scope is unrestricted and the authority wall is skipped (no caller authority
    to clear) — matching the existing pod_fetch behavior for routine/dayflow callers."""
    scope_obj = as_scope_object(scope)
    pod = PodStore().get(pod_id)
    if pod is None:
        raise PodNotFound(pod_id)
    allowed = resolve_allowed_scopes(scope_obj)
    if not pod_in_scope(pod.scope_id, allowed):
        raise PodNotFound(pod_id)
    if scope_obj is not None:
        check_authority(pod_id=pod_id, projection=None, required=pod_min_authority(pod), scope=scope_obj)
    meta = pod.metadata or {}
    return {
        "pod_id": pod.pod_id,
        "kind": pod.kind,
        "one_liner": pod.one_liner or "",
        "body": pod.body or "",
        "tags": list(pod.tags or []),
        "created_at": getattr(pod, "created_at", "") or "",
        "source_urls": list(meta.get("source_urls") or []),
        "scope_id": pod.scope_id,
    }
