"""
Primary user identity constants for the knowledge graph.

All code that needs to identify the primary user node by name should import
from here rather than hardcoding the name.  This is the single place that
needs to change when the system is deployed for a different user.

Resolution order:
  1. PRIMARY_USER environment variable (set via UI settings page)
  2. resources/user/resource_user_data.json  preferred_name / first_name
  3. Hard fallback: "User"
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_primary_user_name() -> str:
    env_val = os.environ.get("PRIMARY_USER", "").strip()
    if env_val:
        return env_val

    try:
        resource_path = get_resources_dir() / "user" / "resource_user_data.json"
        if resource_path.exists():
            data = json.loads(resource_path.read_text(encoding="utf-8"))
            name = (data.get("preferred_name") or data.get("first_name") or "").strip()
            if name:
                return name
    except Exception:
        logger.error(
            "Failed to read primary user name from resource_user_data.json — "
            "falling back to 'User'. KG node anchoring may be incorrect.",
            exc_info=True,
        )
    return "User"


def get_primary_user_full_name() -> str:
    try:
        resource_path = get_resources_dir() / "user" / "resource_user_data.json"
        if resource_path.exists():
            data = json.loads(resource_path.read_text(encoding="utf-8"))
            name = (data.get("full_name") or "").strip()
            if name:
                return name
    except Exception:
        logger.error(
            "Failed to read primary user full name from resource_user_data.json — "
            "falling back to preferred name.",
            exc_info=True,
        )
    return get_primary_user_name()


# Module-level constant — use this for node label comparisons throughout the codebase.
# Re-read at import time so tests can override resource_user_data.json before importing.
PRIMARY_USER_NODE_LABEL: str = get_primary_user_name()
