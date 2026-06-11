from pathlib import Path
import fnmatch

from app.assistant.utils.path_utils import get_repo_root


def is_allowed(resolved: Path, allowed: list[str] | None) -> bool:
    """Repo-relative path allowlist check shared by the text-file tools.

    `allowed` entries are repo-relative paths (exact match or fnmatch glob);
    an empty/None list means everything under the repo root is allowed.
    """
    if not allowed:
        return True
    try:
        rel = resolved.relative_to(get_repo_root()).as_posix()
    except Exception:
        return False
    for entry in allowed:
        if not isinstance(entry, str) or not entry.strip():
            continue
        ent = entry.strip().replace("\\", "/")
        # Exact match or glob match.
        if ent == rel or fnmatch.fnmatch(rel, ent):
            return True
    return False
