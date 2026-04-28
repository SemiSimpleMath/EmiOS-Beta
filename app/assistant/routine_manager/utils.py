from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir
from app.assistant.utils.time_utils import get_local_timezone

logger = get_logger(__name__)


def resources_dir() -> Path:
    return get_resources_dir()


def memory_dir() -> Path:
    return resources_dir() / "memory"


def pointers_dir() -> Path:
    return resources_dir() / "pointers"


def status_dir() -> Path:
    return resources_dir() / "status"


def dayflow_outputs_dir() -> Path:
    return resources_dir() / "dayflow_pipeline_outputs"


def daily_insights_outputs_dir() -> Path:
    return resources_dir() / "daily_insights_pipeline_outputs"


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to read JSON file: %s (%s)", path, e)
        return None


def write_json_file(path: Path, data: Dict[str, Any]) -> None:
    """Atomic JSON write: write to temp file, fsync, then rename.

    On Windows, os.replace can fail with PermissionError if another thread
    has the target file open momentarily. Retry a few times with a short
    backoff before propagating.
    """
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp_path, str(path))
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json_optional(path: Path) -> Any:
    if not path.exists():
        return None
    if path.suffix.lower() == ".json":
        return read_json_file(path)
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


from app.assistant.utils.time_utils import parse_iso_utc as parse_iso_utc  # noqa: F401 — re-export


def write_latest_pointer_json(
    *,
    resource_filename: str,
    date_str: str,
    archive_path: Path,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "date": date_str,
        "timezone": str(get_local_timezone()),
        "generated_at_utc": utc_now().isoformat(),
        "archive_path": str(archive_path),
    }
    if extra:
        payload.update(extra)
    write_json_file(pointers_dir() / resource_filename, payload)

