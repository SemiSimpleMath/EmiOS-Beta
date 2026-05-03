"""Generic file ingestion — content-addressed storage + pod minting for any file.

Image-shaped files (jpg/png/etc.) keep their existing rich path through
``image_ingest.ingest_image_file`` (Pillow probe, vision-extraction tag,
``data/images/`` storage). Everything else lands in ``data/pods/`` with a
minimal, kind-tagged pod.

Storage shape for non-image pods:
  ``data/pods/<sha256[:2]>/<sha256><ext>``

Pod shape:
  - ``kind`` derived from MIME top-level slice: image / video / audio /
    document / file (catch-all). Media-type subtype goes in
    ``metadata.mime_type`` so callers can do precise filtering
    (e.g. only PDFs, only mp4s) without parsing kind.
  - ``one_liner`` = ``"[<kind>: <ext>, <KB>kb] <basename>"`` — structural
    placeholder, swap for a richer description if downstream extraction runs.
  - ``body`` empty until an extraction pass populates it.
  - ``metadata`` carries sha256, file size, source path, mime type, kind.

Idempotent: re-ingesting the same bytes returns the existing pod.
"""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_PODS_DIR_NAME = "pods"
_IMAGE_MIME_PREFIX = "image/"


def _pods_root() -> Path:
    root = get_repo_root() / "data" / _PODS_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _store_path_for(sha256: str, ext: str) -> Path:
    ext = (ext or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return _pods_root() / sha256[:2] / f"{sha256}{ext}"


def _kind_from_mime(mime: Optional[str]) -> str:
    """Map mime top-level slice to a pod kind.

    'image/jpeg' → 'image', 'video/mp4' → 'video', 'audio/mpeg' → 'audio',
    'application/pdf' / 'text/plain' → 'document', everything else → 'file'.
    """
    if not mime:
        return "file"
    head = mime.split("/", 1)[0]
    if head in ("image", "video", "audio"):
        return head
    if head in ("application", "text"):
        return "document"
    return "file"


def _make_file_pod_id(kind: str, sha256: str) -> str:
    return f"datapod:{kind}:{sha256[:24]}"


def ingest_file(
    *,
    src_path: Path,
    source_kind: str = "manual_upload",
    pod_store: Optional[PodStore] = None,
    metadata_extra: Optional[Dict[str, Any]] = None,
    occurred_at_utc: Optional[datetime] = None,
) -> Pod:
    """Ingest one file (any kind): hash, content-address, mint pod, return.

    Image-shaped files are routed through the existing image_ingest path so
    they pick up Pillow probing and vision-extraction wiring. Everything else
    follows the generic path here.

    Idempotent — re-ingesting the same bytes returns the existing pod.
    """
    src_path = Path(src_path).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"file not found: {src_path}")

    mime, _enc = mimetypes.guess_type(src_path.name)
    if mime and mime.startswith(_IMAGE_MIME_PREFIX):
        # Delegate to the rich image path so dims / vision-extract slots line up.
        from app.assistant.pod_store.image_ingest import ingest_image_file
        return ingest_image_file(
            src_path=src_path,
            source_kind=source_kind,
            pod_store=pod_store,
            metadata_extra=metadata_extra,
            occurred_at_utc=occurred_at_utc,
        )

    ext = src_path.suffix.lower()
    sha256 = _sha256_file(src_path)
    kind = _kind_from_mime(mime)
    dest = _store_path_for(sha256, ext)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)
        logger.info("[file_ingest] stored %s -> %s", src_path.name, dest.relative_to(get_repo_root()))
    else:
        logger.debug("[file_ingest] already in store: %s", dest.name)

    store = pod_store or PodStore()
    pod_id = _make_file_pod_id(kind, sha256)
    existing = store.get(pod_id)
    if existing is not None:
        logger.debug("[file_ingest] pod %s already minted, returning existing", pod_id[:30])
        return existing

    file_size = dest.stat().st_size
    if occurred_at_utc is None:
        try:
            occurred_at_utc = datetime.fromtimestamp(src_path.stat().st_mtime, tz=timezone.utc)
        except Exception:
            occurred_at_utc = datetime.now(timezone.utc)

    one_liner = f"[{kind}: {ext.lstrip('.') or '?'}, {file_size // 1024}kb] {src_path.name}"

    metadata: Dict[str, Any] = {
        "sha256": sha256,
        "ext": ext,
        "mime_type": mime,
        "file_size_bytes": file_size,
        "stored_path": str(dest.relative_to(get_repo_root())),
        "source_path": str(src_path),
        "source_kind": source_kind,
        "occurred_at_utc": occurred_at_utc.astimezone(timezone.utc).isoformat() if occurred_at_utc else None,
    }
    if metadata_extra:
        metadata.update(metadata_extra)

    pod = Pod(
        pod_id=pod_id,
        kind=kind,
        tags=[],
        one_liner=one_liner,
        body="",
        source_refs=[PodSourceRef(kind="image_file", id=sha256)],
        for_agents=[],
        scope_id=None,
        created_by="file_ingest",
        metadata=metadata,
    )
    store.put(pod)

    logger.info("[file_ingest] minted %s pod %s sha256=%s... %skb %s",
                kind, pod_id, sha256[:12], file_size // 1024, src_path.name)
    return pod
