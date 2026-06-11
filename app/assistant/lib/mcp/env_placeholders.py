from __future__ import annotations

import os
import re
from typing import Any


def resolve_env_placeholders(raw_env: dict[str, Any]) -> dict[str, str]:
    """Expand ``${VAR}`` placeholders in MCP launch env values from os.environ.

    Raises ValueError when a referenced environment variable is missing.
    """
    out: dict[str, str] = {}
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for k, v in raw_env.items():
        key = str(k)
        val = str(v)
        matches = pattern.findall(val)
        if not matches:
            out[key] = val
            continue
        resolved = val
        for env_key in matches:
            env_val = os.environ.get(env_key)
            if env_val is None:
                raise ValueError(
                    f"Missing required environment variable '{env_key}' for MCP launch env key '{key}'."
                )
            resolved = resolved.replace(f"${{{env_key}}}", env_val)
        out[key] = resolved
    return out
