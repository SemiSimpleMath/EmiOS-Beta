"""/api/pods/&lt;pod_id&gt;/image — serve image-pod bytes for inline chat rendering.

The chat UI emits messages that may reference image pods via
`datapod:image:<id>` URIs. The renderer rewrites those to <img> tags
pointing at this endpoint, which streams the underlying file.

Safety:
- Only pods with kind == "image" are served (chat-displayable types only).
- The pod's stored_path must resolve to a path UNDER the repo root.
  Anything escaping (symlinks, absolute paths outside the repo) is
  rejected with 403 — same path-traversal guard as serve_uploads_temp.
- 404 when the pod is missing OR not an image OR the file is gone.
"""
import mimetypes

from flask import Blueprint, abort, jsonify, send_file

from app.assistant.pod_store.pod_store import PodStore
from app.assistant.pod_store.pod_uri import POD_URI_RE
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

pod_api_bp = Blueprint("pod_api", __name__)

# Fail-closed allowlist of pod kinds whose contents may be served to the chat
# UI. ONLY add kinds that hold non-sensitive, user-displayable output. Secret /
# credential / identity pods must never appear here — anything not listed 404s,
# so the default is "not displayable". Mirrors the kind=="image" guard below.
DISPLAYABLE_POD_KINDS = {"research_finding"}


@pod_api_bp.route("/api/pods/<pod_id>")
def get_pod_contents(pod_id: str):
    """Return a displayable pod's contents as JSON for the chat pod-viewer.

    Two fences: (1) the read goes through the universal gate
    (``pod_utils.read_pod_gated``) using the owner web UI's master_room scope, so
    the route respects pod scope + authority like every other surface (a 100-band
    courier pod, or a pod outside the owner's scope, is denied); (2) only kinds in
    DISPLAYABLE_POD_KINDS are served — so secret/credential/identity pods 404 even
    if the gate would pass them. The pod_id must be a well-formed pod URI.
    """
    pod_id_clean = (pod_id or "").strip()
    if not POD_URI_RE.fullmatch(pod_id_clean):
        abort(404)

    from app.assistant.pod_store import pod_utils
    from app.assistant.pod_store.authority import PodAuthorityError
    from app.assistant.room_session_manager.services.pod_command import build_room_scope

    try:
        scope = build_room_scope("master_room", "ui")  # the owner's web UI surface
        pod = pod_utils.read_pod_gated(pod_id_clean, scope)
    except (pod_utils.PodNotFound, PodAuthorityError):
        # Missing / out of scope / insufficient authority — one response, no leak.
        abort(404)
    except Exception as e:
        logger.warning("[pod_api] read_pod_gated failed for %s: %s", pod_id_clean, e)
        abort(500)

    if pod.get("kind") not in DISPLAYABLE_POD_KINDS:
        abort(404)

    return jsonify({
        "pod_id": pod["pod_id"],
        "kind": pod["kind"],
        "one_liner": pod["one_liner"],
        "body": pod["body"],
        "tags": pod.get("tags") or [],
        "created_at": pod.get("created_at") or "",
        "source_urls": pod.get("source_urls") or [],
    })


@pod_api_bp.route("/api/pods/<path:pod_id>/image")
def get_pod_image(pod_id: str):
    """Stream the bytes of an image pod for inline chat rendering."""
    pod_id_clean = (pod_id or "").strip()
    if not pod_id_clean.startswith("datapod:image:"):
        # Either bad shape OR pod is some other kind we won't serve here.
        # Treat as not-found to avoid leaking which non-image pods exist.
        abort(404)

    try:
        store = PodStore()
        pod = store.get(pod_id_clean)
    except Exception as e:
        logger.warning("[pod_api] PodStore.get failed for %s: %s", pod_id_clean, e)
        abort(500)

    if pod is None:
        abort(404)
    if pod.kind != "image":
        abort(404)

    meta = pod.metadata or {}
    rel_path = (meta.get("stored_path") or "").strip()
    if not rel_path:
        logger.warning("[pod_api] pod %s has no stored_path", pod_id_clean)
        abort(404)

    try:
        # Path-traversal guard via relative_to (NOT str.startswith — a sibling like <root>_evil
        # defeats startswith). Raises if stored_path escapes the repo root.
        from app.assistant.utils.path_utils import resolve_repo_child
        target = resolve_repo_child(rel_path)
    except ValueError:
        logger.warning("[pod_api] pod %s stored_path escapes repo root: %s", pod_id_clean, rel_path)
        abort(403)
    if not target.is_file():
        logger.warning("[pod_api] pod %s file missing on disk: %s", pod_id_clean, target)
        abort(404)

    ctype, _ = mimetypes.guess_type(str(target))
    if not ctype or not ctype.startswith("image/"):
        # Defense in depth — refuse to serve as image if mime says otherwise
        ctype = "application/octet-stream"

    return send_file(
        str(target),
        mimetype=ctype,
        max_age=300,  # 5min browser cache; pods are immutable once minted
        conditional=True,
    )
