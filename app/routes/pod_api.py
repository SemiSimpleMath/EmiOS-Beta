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
from pathlib import Path

from flask import Blueprint, abort, jsonify, send_file

from app.assistant.pod_store.pod_store import PodStore
from app.assistant.pod_store.pod_uri import POD_URI_RE
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

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

    Fail-closed: only pods whose kind is in DISPLAYABLE_POD_KINDS are served;
    everything else (including secret/credential/identity pods) 404s. The
    pod_id must also be a well-formed pod URI, so this never doubles as a
    generic file/secret read.
    """
    pod_id_clean = (pod_id or "").strip()
    if not POD_URI_RE.fullmatch(pod_id_clean):
        abort(404)

    try:
        store = PodStore()
        pod = store.get(pod_id_clean)
    except Exception as e:
        logger.warning("[pod_api] PodStore.get failed for %s: %s", pod_id_clean, e)
        abort(500)

    if pod is None or pod.kind not in DISPLAYABLE_POD_KINDS:
        # Not-found OR not a displayable kind — same response either way so we
        # don't leak which non-displayable pods exist.
        abort(404)

    meta = pod.metadata or {}
    return jsonify({
        "pod_id": pod.pod_id,
        "kind": pod.kind,
        "one_liner": pod.one_liner or "",
        "body": pod.body or "",
        "tags": list(pod.tags or []),
        "created_at": getattr(pod, "created_at", "") or "",
        "source_urls": list(meta.get("source_urls") or []),
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

    repo_root = Path(get_repo_root()).resolve()
    target = (repo_root / rel_path).resolve()
    if not str(target).startswith(str(repo_root)):
        # Path-traversal guard
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
