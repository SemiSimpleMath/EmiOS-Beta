"""Image ingestion — content-addressed storage + image-pod minting.

Storage shape: ``data/images/<hash[:2]>/<hash>.<ext>``
- SHA256 of the bytes is the canonical id (deduplication is automatic).
- Two-char prefix dirs keep file-system fanout reasonable.
- Extension is preserved for ergonomics (so a later viewer can serve
  the file with the right mimetype).

Pod shape:
- ``kind="image"``
- ``one_liner`` = vision caption when extraction has run; placeholder
  ("[image: <ext>, <KB>kb, <WxH>]") until then.
- ``body`` = vision caption + OCR text once an extraction pass runs.
  Empty until then. Search via ``pod_search?kind=image&query=...`` will
  pick up whatever's in the body.
- ``source_refs`` = ``[{kind: "image_file", id: "<sha256>"}]``
- ``metadata`` carries width / height / format / file_size_bytes /
  source_path (where it came from) / source_kind (chat_attachment,
  email_attachment, manual_upload, etc.) so consumers can filter
  without reading the file.

This module deliberately does NOT do the vision-LLM extraction pass.
That's a separate step (image_vision_extractor) the next commit will
add. Splitting the storage layer from the LLM-extraction layer means:
- An image is fully ingested and recoverable the moment its bytes hit
  disk, even if vision extraction fails or hasn't been wired yet.
- Re-running vision (cheaper prompt, better model) is a pure-update
  pass on existing pods — no need to re-ingest.
- A naked-eye consumer (someone clicking through pods in a viewer UI)
  still gets thumbnails / dimensions / source info from metadata even
  before any LLM has touched the image.
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_IMAGE_DIR_NAME = "images"
_VALID_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp"})


def _images_root() -> Path:
    """Directory where image bytes are stored, content-addressed.

    Sits alongside the existing ``data/`` artifacts (chroma_chat_memory,
    emi.db, execution_traces, etc.). Created on first use.
    """
    root = get_repo_root() / "data" / _IMAGE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_ext(ext: str) -> str:
    """Lowercase + dot-prefixed, e.g. 'JPG' → '.jpg'."""
    e = (ext or "").strip().lower()
    if not e:
        return ""
    if not e.startswith("."):
        e = "." + e
    return e


def _store_path_for(sha256: str, ext: str) -> Path:
    return _images_root() / sha256[:2] / f"{sha256}{_normalize_ext(ext)}"


def _probe_image_dims(path: Path) -> Dict[str, Any]:
    """Best-effort width/height + format probe via Pillow.

    Returns empty dict if Pillow isn't installed or the file isn't a
    decodable image — we don't fail ingestion over a missing thumbnail.
    """
    try:
        from PIL import Image  # noqa: WPS433 (lazy import — Pillow is optional)
    except Exception:
        return {}
    try:
        with Image.open(path) as im:
            return {
                "width": int(im.width),
                "height": int(im.height),
                "format": str(im.format or "").lower() or None,
            }
    except Exception as e:
        logger.debug("[image_ingest] Pillow probe failed for %s: %s", path, e)
        return {}


def _make_image_pod_id(sha256: str) -> str:
    """Deterministic pod_id from the image sha256.

    Mirrors chat / email cluster id shape (datapod:<kind>:<24-hex>) so
    the existing POD_URI_RE matches and so re-ingesting the same bytes
    is naturally idempotent.
    """
    return f"datapod:image:{sha256[:24]}"


def ingest_image_file(
    *,
    src_path: Path,
    source_kind: str = "manual_upload",
    pod_store: Optional[PodStore] = None,
    metadata_extra: Optional[Dict[str, Any]] = None,
    occurred_at_utc: Optional[datetime] = None,
) -> Pod:
    """Ingest one image: hash it, copy into content-addressed storage,
    mint an image pod, return it.

    Idempotent: re-ingesting the same bytes returns the existing pod
    without overwriting on-disk content or pod fields. Callers who
    want to refresh (e.g., re-run vision extraction) should mutate
    pod.body / pod.metadata via PodStore.put() explicitly.

    Args:
        src_path: source file on disk. May live outside the
            ``data/images/`` tree — this function copies it in.
        source_kind: free-form provenance tag — ``chat_attachment``,
            ``email_attachment``, ``camera_roll``, ``manual_upload``, etc.
        pod_store: optional pre-built store; default constructs one.
        metadata_extra: additional metadata to merge into the pod
            (e.g., chat envelope ids, EXIF, captured-by transport).
        occurred_at_utc: when the image was originally created /
            received. Defaults to file mtime, falls back to now.
    """
    src_path = Path(src_path).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"image source not found: {src_path}")

    ext = _normalize_ext(src_path.suffix)
    if ext and ext not in _VALID_IMAGE_EXTS:
        # Don't refuse — just log. Some MMS attachments come through
        # with non-standard extensions; the bytes might still be a
        # valid image. Pillow probe will tell us downstream.
        logger.debug("[image_ingest] unknown image extension %r for %s — proceeding",
                     ext, src_path.name)

    sha256 = _sha256_file(src_path)
    dest = _store_path_for(sha256, ext)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)
        logger.info("[image_ingest] stored %s -> %s", src_path.name, dest.relative_to(get_repo_root()))
    else:
        logger.debug("[image_ingest] image already in store: %s", dest.name)

    store = pod_store or PodStore()
    pod_id = _make_image_pod_id(sha256)
    existing = store.get(pod_id)
    if existing is not None:
        logger.debug("[image_ingest] pod %s already minted, returning existing", pod_id[:30])
        return existing

    dims = _probe_image_dims(dest)
    file_size = dest.stat().st_size
    if occurred_at_utc is None:
        try:
            occurred_at_utc = datetime.fromtimestamp(src_path.stat().st_mtime, tz=timezone.utc)
        except Exception:
            occurred_at_utc = datetime.now(timezone.utc)

    # Until the vision-extraction pass runs, body is empty and the
    # one_liner is a structural placeholder. pod_search will still
    # return the pod by kind=image; substring matching just won't have
    # rich text to match against yet.
    width, height = dims.get("width"), dims.get("height")
    dims_str = f"{width}x{height}" if width and height else "?x?"
    fmt = (dims.get("format") or ext.lstrip(".") or "image").lower()
    one_liner_placeholder = f"[image: {fmt}, {file_size // 1024}kb, {dims_str}]"

    metadata: Dict[str, Any] = {
        "sha256": sha256,
        "ext": ext,
        "format": dims.get("format"),
        "width": width,
        "height": height,
        "file_size_bytes": file_size,
        "stored_path": str(dest.relative_to(get_repo_root())),
        "source_path": str(src_path),
        "source_kind": source_kind,
        "occurred_at_utc": occurred_at_utc.astimezone(timezone.utc).isoformat() if occurred_at_utc else None,
        "vision_extraction_status": "pending",
    }
    if metadata_extra:
        metadata.update(metadata_extra)

    pod = Pod(
        pod_id=pod_id,
        kind="image",
        tags=[],  # populated by vision extraction pass
        one_liner=one_liner_placeholder,
        body="",   # populated by vision extraction pass (caption + OCR text)
        source_refs=[PodSourceRef(kind="image_file", id=sha256)],
        for_agents=[],  # consumers filter by kind="image" + tags + query
        scope_id=None,
        created_by="image_ingest",
        metadata=metadata,
    )
    store.put(pod)

    # Stamp the destination file with its pod_id so a reconciler can
    # find it later regardless of how it gets renamed/moved. See
    # file_stamp.py for the sidecar format. Best-effort: stamp failure
    # logs a warning but doesn't fail ingestion (the content-addressed
    # storage path is enough on its own to recover the pod).
    try:
        from app.assistant.pod_store.file_stamp import write_stamp
        write_stamp(dest, pod_id=pod_id, sha256=sha256, extra={"source_kind": source_kind})
    except Exception as e:
        logger.warning("[image_ingest] could not stamp %s: %s", dest, e)

    logger.info("[image_ingest] minted image pod %s sha256=%s... %s %skb",
                pod_id, sha256[:12], dims_str, file_size // 1024)
    return pod
