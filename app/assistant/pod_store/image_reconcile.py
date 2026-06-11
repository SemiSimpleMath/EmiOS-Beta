"""Reconciler for image pods — walks a directory, syncs pod_store
metadata with what's actually on disk, mints new pods for unstamped
files, flags pods whose backing file has gone missing.

Use cases:

- Curated identity directory (``resources/identity/``): user adds /
  renames / moves photos here over time. The reconciler picks up the
  changes and updates each pod's ``metadata.stored_path``.
- Bulk import (drop a folder of photos somewhere, point the reconciler
  at it): every file gets a pod minted on first scan.
- Health check: verify every image pod still has a backing file at
  its stored_path; report missing assets.

Design intent (see ``project_image_storage_and_stamping`` memo):

The reconciler is the cheap, lazy answer to "user moves files." No
filesystem watcher daemon — this routine runs on demand or on a
weekly cadence. It uses three signals to identify a file, in order:

1. **Sidecar stamp** (``<file>.emipod.json``) — if present, points
   directly at a pod_id.
2. **Content hash (sha256)** — if no stamp, hash the file and look up
   any existing pod with a matching ``metadata.sha256``.
3. **Mint a new pod** — if neither stamp nor hash matches an existing
   pod, treat as a new image, ingest fresh.

Returns a structured summary so callers can log / display the result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.file_stamp import is_sidecar, read_stamp, write_stamp
from app.assistant.pod_store.image_ingest import (
    _VALID_IMAGE_EXTS,
    _normalize_ext,
    _probe_image_dims,
    ingest_image_file,
)
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.hashing import sha256_file
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


def _looks_like_image(path: Path) -> bool:
    if not path.is_file():
        return False
    if is_sidecar(path):
        return False
    return _normalize_ext(path.suffix) in _VALID_IMAGE_EXTS


def _find_pod_by_sha256(store: PodStore, sha256: str) -> Optional[Pod]:
    """Look up an image pod by its content hash.

    Cheap path: pod_id for image pods is ``datapod:image:<sha256[:24]>``.
    We can probe directly without scanning.
    """
    pod_id = f"datapod:image:{sha256[:24]}"
    return store.get(pod_id)


def _update_pod_metadata_path(
    store: PodStore,
    pod: Pod,
    new_stored_path: str,
    new_source_path: Optional[str] = None,
) -> None:
    """Update an existing pod's ``metadata.stored_path`` (and source_path if
    given) and re-put. Used when a file was found at a new location."""
    md = dict(pod.metadata or {})
    md["stored_path"] = new_stored_path
    if new_source_path:
        md["source_path"] = new_source_path
    pod.metadata = md
    store.put(pod)


def reconcile_directory(
    *,
    directory: Path,
    pod_store: Optional[PodStore] = None,
    source_kind: str = "manual_upload",
    mint_unstamped: bool = True,
) -> Dict[str, Any]:
    """Walk ``directory``, reconcile pods with what's on disk.

    For each image file under ``directory``:
      - **Stamped + pod exists**: update ``metadata.stored_path`` if the
        file is at a new location relative to what the pod records.
        Stamp passes through unchanged.
      - **Stamped + pod missing**: pod was deleted but the file survived.
        Re-mint as a fresh pod (sha256 may match an unrelated existing
        pod via content addressing — that's fine, one pod per content).
      - **Unstamped + pod exists by content**: file was added without
        going through ``image_ingest``. Stamp the file + relink.
      - **Unstamped + no pod**: brand-new file. Mint a new pod via
        ``ingest_image_file`` if ``mint_unstamped=True``.
      - **Sidecar present but main file missing**: log as orphan
        sidecar; don't act.

    Args:
        directory: where to walk. Recursive.
        pod_store: optional pre-built store (default constructs one).
        source_kind: tag for newly-minted pods this run produces.
        mint_unstamped: if False, only repair existing pods; don't
            mint pods for files we've never seen. Useful for health
            checks where minting is undesirable.

    Returns a summary dict with counts + lists of affected paths.
    """
    directory = Path(directory).resolve()
    if not directory.is_dir():
        return {
            "scanned": 0,
            "error": f"directory not found: {directory}",
        }

    store = pod_store or PodStore()
    repo_root = get_repo_root()

    summary = {
        "scanned": 0,
        "path_updated": [],     # pod was found, stored_path got refreshed
        "stamp_added": [],      # file matched a pod by sha256, sidecar created
        "minted": [],           # new pod created for previously-unseen file
        "orphan_sidecars": [],  # sidecar present, main file missing
        "stamped_pod_missing": [],  # file says it's pod X but pod X is gone
        "skipped": [],          # not an image (and not a sidecar — those silently skip)
    }

    # First pass: detect orphan sidecars (sidecar with no main file).
    seen_main_files: set[str] = set()
    sidecars: List[Path] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if is_sidecar(path):
            sidecars.append(path)
            continue
        if _looks_like_image(path):
            seen_main_files.add(str(path))

    for sc in sidecars:
        # sidecar is at <file>.emipod.json; main file is the path without
        # the suffix.
        main_name = sc.name[: -len(".emipod.json")]
        main_path = sc.with_name(main_name)
        if not main_path.is_file():
            summary["orphan_sidecars"].append(str(sc))

    # Second pass: walk image files.
    for path in directory.rglob("*"):
        if not path.is_file() or is_sidecar(path):
            continue
        if not _looks_like_image(path):
            summary["skipped"].append(str(path))
            continue
        summary["scanned"] += 1

        resolved = path.resolve()
        try:
            rel_stored_path = str(resolved.relative_to(repo_root))   # repo-relative when inside
        except ValueError:
            rel_stored_path = str(resolved)                          # else keep the absolute path

        stamp = read_stamp(path)
        stamped_pod_id = (stamp or {}).get("pod_id")
        stamped_sha256 = (stamp or {}).get("sha256")

        if stamped_pod_id:
            pod = store.get(stamped_pod_id)
            if pod is None:
                summary["stamped_pod_missing"].append(stamped_pod_id)
                # Pod was deleted but file (with stamp) remains. Recover by
                # treating the file as new and re-ingesting — a fresh pod
                # gets minted at the same pod_id (sha256 is identity), so
                # this is idempotent and we end up linked again.
                if mint_unstamped:
                    new_pod = ingest_image_file(
                        src_path=path, source_kind=source_kind, pod_store=store,
                    )
                    summary["minted"].append(new_pod.pod_id)
                continue

            # Stamped + pod exists — only update path if drifted.
            current_md_path = (pod.metadata or {}).get("stored_path")
            if current_md_path != rel_stored_path:
                _update_pod_metadata_path(
                    store, pod,
                    new_stored_path=rel_stored_path,
                    new_source_path=str(path.resolve()),
                )
                summary["path_updated"].append({
                    "pod_id": pod.pod_id, "old": current_md_path, "new": rel_stored_path,
                })
            continue

        # Unstamped — content-addressed lookup.
        sha256 = sha256_file(path)
        existing = _find_pod_by_sha256(store, sha256)
        if existing is not None:
            # Pod exists for this content; stamp the file + refresh path.
            try:
                write_stamp(path, pod_id=existing.pod_id, sha256=sha256,
                            extra={"reconciled_from_path": str(path.resolve())})
                summary["stamp_added"].append(str(path))
            except Exception as e:
                logger.warning("[reconcile] could not stamp %s: %s", path, e)

            current_md_path = (existing.metadata or {}).get("stored_path")
            if current_md_path != rel_stored_path:
                _update_pod_metadata_path(
                    store, existing,
                    new_stored_path=rel_stored_path,
                    new_source_path=str(path.resolve()),
                )
                summary["path_updated"].append({
                    "pod_id": existing.pod_id, "old": current_md_path, "new": rel_stored_path,
                })
            continue

        # Brand new file — mint a pod.
        if mint_unstamped:
            new_pod = ingest_image_file(
                src_path=path, source_kind=source_kind, pod_store=store,
            )
            summary["minted"].append(new_pod.pod_id)

    return summary
