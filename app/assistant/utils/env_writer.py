"""Safe upsert of KEY=VALUE lines in a .env-style file.

Used by the credential-mint UI to persist secrets + pod_ids into .env
when the user configures a new account through `/settings/accounts`. Same
process also calls `os.environ[KEY] = value` so the running interpreter
sees the new value without a restart; this module only handles the
on-disk side.

Design properties:

  - Atomic write (write to tempfile in same dir, then os.replace).
  - Preserve existing line ordering. Existing key → in-place value swap;
    new key → append at end (with a trailing newline if missing).
  - Preserve comments and blank lines untouched.
  - Never log the value — only the key — even on error.
  - Quoting: leave the value literal (no shell-escaping). dotenv accepts
    bare values that don't contain whitespace / special chars; if a
    caller passes a value that does, they're responsible for quoting it.
    For pod_ids and secrets we mint here, neither contains whitespace.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


def _key_line(line: str) -> Optional[str]:
    """Return the KEY if `line` is a KEY=... assignment, else None."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key or None


def upsert_env(key: str, value: str, *, env_path: Optional[Path] = None) -> bool:
    """Set `KEY=value` in the .env file, in-place if present, appended if not.

    Returns True if the value changed (or was newly added), False if the
    file already had the same value.
    """
    if not key or not isinstance(key, str):
        raise ValueError("env_writer.upsert_env: key must be a non-empty string")
    if "\n" in key or "=" in key:
        raise ValueError(f"env_writer.upsert_env: key {key!r} contains illegal characters")
    if value is None:
        raise ValueError("env_writer.upsert_env: value must not be None")
    if "\n" in value:
        raise ValueError(f"env_writer.upsert_env: value for {key!r} contains a newline")

    path = env_path or (get_repo_root() / ".env")
    if not path.exists():
        # Initialize an empty .env if missing — caller probably installed
        # the app without copying .env.example. Better than failing the whole
        # mint flow on a setup gap.
        path.write_text("", encoding="utf-8")

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=False)

    new_assignment = f"{key}={value}"
    replaced = False
    changed = False
    for i, line in enumerate(lines):
        if _key_line(line) == key:
            if line == new_assignment:
                logger.info("env_writer: %s already set to current value; no-op", key)
                return False
            lines[i] = new_assignment
            replaced = True
            changed = True
            break

    if not replaced:
        lines.append(new_assignment)
        changed = True

    new_text = "\n".join(lines)
    if original.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    if not original.endswith("\n") and not replaced:
        # Ensure there's a separating newline before our appended line.
        # Without this, an existing trailing-newline-less file could end up
        # concatenating its last line with our new KEY= assignment.
        if original and not original.endswith("\n"):
            new_text = original + ("\n" if not original.endswith("\n") else "") + new_assignment + "\n"

    # Atomic write — temp file in same dir + rename.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".env_tmp_", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info("env_writer: %s %s in %s", key, "updated" if replaced else "added", path.name)
    return changed
