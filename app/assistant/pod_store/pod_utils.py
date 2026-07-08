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


def _slug_kind(kind: str) -> str:
    """Kind slug that PRESERVES dots — the dot is the kind-namespace separator
    (``intention.meal``, ``auth.bearer``); everything else slugs to snake_case
    per segment. The result matches the kind grammar in ``pod_uri``."""
    segments = [seg for seg in str(kind or "").strip().lower().split(".") if seg]
    return ".".join(_slug(seg) for seg in segments) or "x"


def canonical_pod_id(kind: str, *parts: Any) -> str:
    """Build a canonical pod id ``datapod:<kind>:<12-hex token>`` where the token is a
    deterministic hash of ``parts`` (re-minting the same logical unit upserts ONE pod, and the
    id always matches ``pod_uri.POD_URI_RE`` so the PodInjector and the chat linkifier recognize
    it). Dotted kinds keep their dots (``auth.bearer``). Use this instead of hand-formatting
    pod ids — that is the bug the audit caught, twice."""
    kind_slug = _slug_kind(kind)
    raw = "|".join(str(p) for p in parts) if parts else kind_slug
    token = hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()
    pod_id = f"datapod:{kind_slug}:{token}"
    if not POD_URI_RE.fullmatch(pod_id):
        raise ValueError(f"canonical_pod_id produced a non-canonical id: {pod_id!r}")
    return pod_id


def mint_pod(
    *,
    kind: str,
    one_liner: str,
    created_by: str,
    variant: "Optional[str]" = None,
    body: "Optional[str]" = None,
    tags: "Optional[List[str]]" = None,
    metadata: "Optional[Dict[str, Any]]" = None,
    scope_id: "Optional[str]" = None,
    source_refs: "Optional[list]" = None,
    for_agents: "Optional[List[str]]" = None,
    min_authority: "Optional[int]" = None,
    importance: "Optional[float]" = None,
    identity: "Optional[List[Any]]" = None,
    store: "Optional[PodStore]" = None,
) -> str:
    """THE unified content-pod minter — one id builder, one tag assembly, one
    write path, so no minter re-invents the format (put() still enforces it;
    this is the constructor every lane shares so the gate never fires).

    - ``variant`` is the governed routing tag for family kinds (intention/
      plan/feedback); it is prepended to ``tags`` automatically. Role tags
      (e.g. ``run_summary``) and extras ride in ``tags``.
    - ``identity``: deterministic parts hashed into the id — re-minting the
      same logical unit upserts ONE pod. CAUTION: put() replaces metadata
      wholesale, so only use identity where later metadata stamps
      (inventory_applied_at_utc, feedback_asked_at_utc) either don't exist or
      may be reset by a re-mint. Omit for a fresh uuid-backed id per call
      (the lane default).
    - ``scope_id`` is EXPLICIT — the caller states the mint scope rule
      (originating room / account / None = owner-only); the helper never
      guesses.

    Returns the pod_id. Raises whatever put()'s format/registry/variant gate
    raises — minting an unreferenceable pod is always loud.
    """
    import uuid as _uuid
    parts = list(identity) if identity else [_uuid.uuid4().hex]
    pod_id = canonical_pod_id(kind, *parts)

    assembled_tags: List[str] = []
    if variant:
        assembled_tags.append(str(variant))
    for t in (tags or []):
        t_str = str(t).strip()
        if t_str and t_str not in assembled_tags:
            assembled_tags.append(t_str)

    pod = Pod(
        pod_id=pod_id,
        kind=kind,
        tags=assembled_tags,
        one_liner=one_liner,
        body=body,
        source_refs=list(source_refs or []),
        for_agents=list(for_agents or []),
        scope_id=scope_id,
        created_by=created_by,
        metadata=dict(metadata or {}),
        min_authority=min_authority,
        importance=importance,
    )
    (store or PodStore()).put(pod)
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


# The map of "what is SYSTEM" — the owner's own surfaces. Add a room_id here to
# fold it into the system scope. The owner drives master_room directly;
# dayflow_orchestrator is that same owner's autonomous engine — it acts on their
# behalf at authority 95 (vs master_room's 99) and escalates to master_room for
# approval on truly important actions. They are peers, not parent/child. A pod
# minted by any system surface is the owner's data and is readable from every
# system surface — they share one pod-visibility identity.
#
# This lives in the pod-scope SSOT because pod visibility is its only consumer
# today; if "system" grows to gate resources/authority too, lift it to the scope
# layer. Prefer is_system_scope() over comparing room ids in callers.
SYSTEM_SCOPES = frozenset({"master_room", "dayflow_orchestrator"})


def is_system_scope(scope_id: Any) -> bool:
    """True if ``scope_id`` is one of the owner's system-level surfaces."""
    return isinstance(scope_id, str) and scope_id in SYSTEM_SCOPES


def _expand_system_scopes(scope_id: str) -> set:
    """A system surface resolves to the WHOLE system set (system surfaces are
    mutually pod-visible); any other scope resolves to just itself."""
    return set(SYSTEM_SCOPES) if scope_id in SYSTEM_SCOPES else {scope_id}


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
    if not out:
        return ["__none__"]
    # A caller on a system surface (master_room / dayflow_orchestrator) reads
    # pods minted by ANY system surface — they share one identity. Expand each
    # concrete scope through the system map. No-op while system rooms grant
    # pods:[all] (returned above), but keeps the equivalence true if either is
    # ever narrowed to "self".
    expanded: List[str] = []
    for s in out:
        for eq in sorted(_expand_system_scopes(s)):
            if eq not in expanded:
                expanded.append(eq)
    return expanded


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


def get_pod_gated(pod_id: str, scope: Any) -> Pod:
    """THE single-pod dereference gate, returning the full ``Pod`` object.

    Applies the scope wall then the authority wall. Raises ``PodNotFound``
    (missing OR out of scope — indistinguishable on purpose) or
    ``PodAuthorityError`` (in scope but authority below the pod's floor).
    A None scope is a trusted system-internal read: scope is unrestricted and
    the authority wall is skipped (no caller authority to clear).

    Every surface that dereferences a pod on an AGENT'S BEHALF goes through
    this — pod_fetch, /pod expand, /api/pods (via ``read_pod_gated``), and
    send_email's attach/inline paths (which need the Pod's metadata, hence
    this object-returning form)."""
    scope_obj = as_scope_object(scope)
    pod = PodStore().get(pod_id)
    if pod is None:
        raise PodNotFound(pod_id)
    allowed = resolve_allowed_scopes(scope_obj)
    if not pod_in_scope(pod.scope_id, allowed):
        raise PodNotFound(pod_id)
    if scope_obj is not None:
        check_authority(pod_id=pod_id, projection=None, required=pod_min_authority(pod), scope=scope_obj)
    return pod


def read_pod_gated(pod_id: str, scope: Any) -> Dict[str, Any]:
    """``get_pod_gated`` rendered to the dict shape the read surfaces consume:
    ``{pod_id, kind, one_liner, body, tags, created_at, source_urls, scope_id}``.
    Same walls, same exceptions."""
    pod = get_pod_gated(pod_id, scope)
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
