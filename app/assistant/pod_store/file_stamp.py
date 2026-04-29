"""File-stamp read/write — embed a pod_id on an image (or any artifact)
file so a reconciler can identify it later regardless of filename or
path.

v1 uses **sidecar JSON files** as the stamp medium:

  jukka.jpg
  jukka.jpg.emipod.json   ← {"pod_id": "datapod:image:abc...", "sha256": "..."}

Why sidecar (and not xattr / EXIF UserComment / NTFS streams):

- **Portable.** Works on every filesystem on every OS. xattr support is
  uneven (FAT/exFAT/SD cards lose them), and Windows requires NTFS ADS
  which need separate APIs.
- **Inspectable.** A user looking at the directory sees the stamp file
  next to the asset and understands what it does.
- **Survives copy-and-rsync.** Both files travel together on a normal
  ``cp -a`` / ``rsync -a``.

Risks (and what we accept):

- **Sidecar can desync if the user moves only the main file.** The
  reconciler handles this — it falls back to content-addressed lookup
  by sha256, then re-creates the sidecar at the new location.
- **Sidecar pollutes the directory listing.** Acceptable cost for
  portability. A future xattr layer can be added for invisibility on
  filesystems that support it.

Future enhancement: stamp via xattr (when supported) AND sidecar (always),
read whichever is present, prefer xattr because invisible. EXIF UserComment
for JPEGs survives platform handoffs (phone → cloud → laptop) even better
than xattr but is JPEG-only, so sidecar stays as the universal floor.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


SIDECAR_SUFFIX = ".emipod.json"


def _sidecar_path(file_path: Path) -> Path:
    """``image.jpg`` -> ``image.jpg.emipod.json``."""
    return file_path.with_name(file_path.name + SIDECAR_SUFFIX)


def read_stamp(file_path: Path) -> Optional[Dict[str, Any]]:
    """Read the sidecar stamp for a file. Returns the parsed JSON dict,
    or None if no sidecar exists or the sidecar is malformed.

    Expected stamp shape::
        {"pod_id": "datapod:image:abc...", "sha256": "abc...", "stamped_at_utc": "..."}

    Extra keys are preserved on read (callers can stash custom data).
    """
    side = _sidecar_path(file_path)
    if not side.is_file():
        return None
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.debug("[file_stamp] sidecar %s is not a dict: %r", side, data)
            return None
        return data
    except Exception as e:
        logger.debug("[file_stamp] failed to read sidecar %s: %s", side, e)
        return None


def write_stamp(
    file_path: Path,
    *,
    pod_id: str,
    sha256: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a sidecar stamp next to ``file_path``. Overwrites if present.

    Returns the sidecar's path (handy for tests / cleanup).
    """
    from datetime import datetime, timezone

    if not pod_id:
        raise ValueError("write_stamp: pod_id is required")
    if not sha256:
        raise ValueError("write_stamp: sha256 is required")

    payload: Dict[str, Any] = {
        "pod_id": pod_id,
        "sha256": sha256,
        "stamped_at_utc": datetime.now(timezone.utc).isoformat(),
        "stamp_version": 1,
    }
    if extra:
        payload.update(extra)

    side = _sidecar_path(file_path)
    side.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("[file_stamp] wrote sidecar %s", side)
    return side


def remove_stamp(file_path: Path) -> bool:
    """Delete the sidecar for a file. Returns True if a sidecar was
    deleted, False if there was nothing to remove.
    """
    side = _sidecar_path(file_path)
    if side.is_file():
        side.unlink()
        return True
    return False


def is_sidecar(path: Path) -> bool:
    """True if the path itself is a sidecar (e.g., when walking a
    directory and you want to skip these from the asset list)."""
    return path.name.endswith(SIDECAR_SUFFIX)
