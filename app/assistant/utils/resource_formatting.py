from __future__ import annotations

import json
from typing import Any

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def format_resource_value(value: Any) -> str:
    """Render a resolved resource value for prompt injection.

    None -> "", strings are stripped, everything else is JSON-encoded.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=2).strip()
    except Exception:
        logger.debug("Failed JSON-encoding resource value for prompt rendering.", exc_info=True)
        return str(value).strip()
