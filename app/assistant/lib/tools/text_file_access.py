from pathlib import Path
import fnmatch

from app.assistant.utils import resource_files
from app.assistant.utils.path_utils import get_repo_root


def resolve_tool_path(path_str: str) -> tuple[Path, str] | None:
    """Resolve a text-file tool's ``file_path`` and REQUIRE containment.

    Tools name files repo-relative (``resources/...``, ``outputs/...``). A path under
    ``resources/`` is the owner's resource tree, so it resolves into the user resources
    root (the data dir when EMI_DATA_DIR is set); everything else resolves under the repo
    root. Returns ``(path, logical)`` where ``logical`` is the repo-relative POSIX name the
    allowlists are written in, or None on traversal/escape (the tool refuses, fail-closed).
    """
    try:
        repo = get_repo_root().resolve()
        user = resource_files.user_root().resolve()
        raw = Path(path_str)
        if raw.is_absolute():
            resolved = raw.resolve()
            try:
                return resolved, (Path("resources") / resolved.relative_to(user)).as_posix()
            except ValueError:
                return resolved, resolved.relative_to(repo).as_posix()
        resolved = (repo / raw).resolve()
        rel = resolved.relative_to(repo)
        if rel.parts and rel.parts[0] == "resources":
            inner = Path(*rel.parts[1:]) if len(rel.parts) > 1 else Path()
            target = (user / inner).resolve()
            target.relative_to(user)
            return target, rel.as_posix()
        return resolved, rel.as_posix()
    except (ValueError, OSError):
        return None


def is_allowed(logical: str, allowed: list[str] | None) -> bool:
    """Repo-relative path allowlist check shared by the text-file tools.

    `logical` is the name resolve_tool_path returns. `allowed` entries are
    repo-relative paths (exact match or fnmatch glob); an empty/None list
    means everything resolve_tool_path accepts is allowed.
    """
    if not allowed:
        return True
    for entry in allowed:
        if not isinstance(entry, str) or not entry.strip():
            continue
        ent = entry.strip().replace("\\", "/")
        # Exact match or glob match.
        if ent == logical or fnmatch.fnmatch(logical, ent):
            return True
    return False
