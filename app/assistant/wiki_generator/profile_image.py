"""Find and materialize the canonical profile image for an EmiPedia entity.

Looks up an outgoing image-pod edge from the entity (preferring
``has_profile_image`` over ``depicted_in``), copies the underlying file
into the wiki vault under ``images/`` so Obsidian/markdown viewers can
render it inline, and returns a path suitable for embedding from the
prose page (which lives at ``<vault>/prose/<entity>.md``, so the
returned reference is ``../images/<file>``).

The destination filename is content-addressed (the same sha256-named
file the pod store wrote), so re-runs are idempotent: if the file is
already in the vault, no copy happens.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_PROFILE_EDGES_PRIMARY = ["has_profile_image"]
_PROFILE_EDGES_FALLBACK = ["depicted_in"]


def find_profile_image_pod(entity_label: str) -> Optional[Pod]:
    """Return the best image pod to use as the entity's profile picture.

    Preference order: ``has_profile_image`` (canonical, intent-flavored)
    → ``depicted_in`` (any picture the entity appears in, most recent
    first). Returns ``None`` if neither yields an image pod.
    """
    store = PodStore()
    for relations in (_PROFILE_EDGES_PRIMARY, _PROFILE_EDGES_FALLBACK):
        hits = store.query(
            kind="image",
            linked_to_entity=entity_label,
            linked_via=relations,
            limit=1,
        )
        if hits:
            return hits[0]
    return None


def materialize_profile_image_for_vault(
    entity_label: str,
    vault_path: Path,
) -> Optional[str]:
    """Copy the entity's profile image into ``<vault>/images/`` and return
    the markdown-relative reference (e.g. ``images/<hash>.png``).

    Returns ``None`` when the entity has no image pod, when the pod
    metadata lacks ``stored_path``, or when the source file is missing.
    """
    pod = find_profile_image_pod(entity_label)
    if pod is None:
        return None

    rel_stored = (pod.metadata or {}).get("stored_path")
    if not rel_stored:
        logger.warning(
            "profile_image: pod %s for entity %r has no metadata.stored_path",
            pod.pod_id, entity_label,
        )
        return None

    src = Path(get_repo_root()) / rel_stored
    if not src.exists():
        logger.warning(
            "profile_image: pod %s stored_path %s missing on disk",
            pod.pod_id, src,
        )
        return None

    images_dir = Path(vault_path) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    dest = images_dir / src.name

    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dest)
        logger.info(
            "profile_image: copied %s → %s for entity %r",
            src.name, dest, entity_label,
        )

    # Prose pages live at <vault>/prose/<entity>.md, so the path needs to
    # climb one directory to reach <vault>/images/.
    return f"../images/{dest.name}"
