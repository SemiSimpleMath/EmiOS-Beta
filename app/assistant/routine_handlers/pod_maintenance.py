"""Routine handlers for pod-store maintenance (2026-07-08 pod audit R4).

Two lanes:
- ``pod_retention_run`` — nightly sweep applying each kind's registry
  retention policy (see pod_store/pod_retention.py). Keeps the store
  bounded so the query layer stays under its scan ceiling.
- ``image_reconcile_run`` — weekly stored_path health check. Walks the
  content-addressed image root (repair-only) and the user-curated
  identity directory (mints new files), so a moved/deleted backing file
  surfaces here instead of at a send_email attach.

Routine JSONs: configs/routines/public/pod_retention.json +
configs/routines/public/image_reconcile.json.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@routine_handler(name="pod_retention_run")
def pod_retention_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Apply the per-kind retention policy from configs/pod_kinds.json."""
    from app.assistant.pod_store.pod_retention import run_pod_retention_sweep

    summary = run_pod_retention_sweep()
    return {"status": "ok", **summary}


@routine_handler(name="image_reconcile_run")
def image_reconcile_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Weekly image-pod health check: sync pod metadata with the files on
    disk, flag pods whose backing file is gone, stamp/mint what the user
    dropped into the identity directory."""
    from app.assistant.pod_store.image_reconcile import reconcile_directory
    from app.assistant.utils.path_utils import get_repo_root

    repo_root = get_repo_root()
    out: Dict[str, Any] = {"status": "ok"}

    images_root = repo_root / "data" / "images"
    if images_root.is_dir():
        # Repair-only over the content-addressed store: refresh drifted
        # stored_paths, report orphan sidecars; never mint from here.
        out["images_root"] = reconcile_directory(
            directory=images_root, mint_unstamped=False,
        )

    identity_dir = repo_root / "resources" / "identity"
    if identity_dir.is_dir():
        # User-curated photos: new/renamed files get stamped + minted.
        out["identity_dir"] = reconcile_directory(
            directory=identity_dir, source_kind="manual_upload", mint_unstamped=True,
        )

    logger.info("[image_reconcile_run] %s", {
        k: (v.get("scanned") if isinstance(v, dict) else v) for k, v in out.items()
    })
    return out
