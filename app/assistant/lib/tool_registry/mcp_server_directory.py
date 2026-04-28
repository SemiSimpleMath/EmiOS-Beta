from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


@dataclass(frozen=True)
class McpDirectoryLoadResult:
    entries_by_id: dict[str, dict[str, Any]]
    errors: list[str]


def _default_servers_dir() -> Path:
    return get_repo_root() / "mcp" / "servers"


def _schema_path() -> Path:
    return get_repo_root() / "mcp" / "schemas" / "mcp_server_entry.schema.json"


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("PyYAML is required to load MCP server entries.") from e

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _basic_validate_entry(doc: dict[str, Any]) -> str | None:
    """
    Minimal validation that does not require optional deps.
    Returns an error message string if invalid, else None.
    """
    required_top = {"schema_version", "server_id", "display_name", "source", "enabled", "launch_options", "policy"}
    missing = [k for k in sorted(required_top) if k not in doc]
    if missing:
        return f"Missing required keys: {missing}"

    if not isinstance(doc.get("schema_version"), int) or doc["schema_version"] < 1:
        return "schema_version must be an integer >= 1"

    import re

    server_id = doc.get("server_id")
    if not isinstance(server_id, str) or not re.match(r"^[a-z0-9]+(\.[a-z0-9-]+)*\/[a-zA-Z0-9._-]+$", server_id):
        return "server_id must look like '<namespace>/<name>', e.g. io.modelcontextprotocol/time or pypi/mcp-github"

    if not isinstance(doc.get("display_name"), str) or not doc["display_name"].strip():
        return "display_name must be a non-empty string"

    if not isinstance(doc.get("enabled"), bool):
        return "enabled must be a boolean"

    if not isinstance(doc.get("launch_options"), list) or len(doc["launch_options"]) < 1:
        return "launch_options must be a non-empty list"

    if not isinstance(doc.get("policy"), dict):
        return "policy must be an object"

    return None


def _try_jsonschema_validate(doc: dict[str, Any]) -> str | None:
    """
    Validate using `mcp/schemas/mcp_server_entry.schema.json` if jsonschema is available.
    Returns an error string if invalid, else None. Returns None if jsonschema is unavailable.
    """
    try:
        from jsonschema import Draft202012Validator  # type: ignore
    except Exception:
        return None

    schema_file = _schema_path()
    if not schema_file.exists():
        return f"Missing schema file: {schema_file}"

    try:
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Failed to parse schema JSON: {e}"

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if not errors:
        return None

    e = errors[0]
    path = ".".join([str(p) for p in e.absolute_path]) or "<root>"
    return f"Schema validation failed at {path}: {e.message}"


def load_mcp_server_directory(servers_dir: str | Path | None = None) -> McpDirectoryLoadResult:
    """
    Load curated MCP server entries from `mcp/servers/**/*.yaml`.

    This loads *metadata only* (no connections are made).
    """
    servers_path = Path(servers_dir) if servers_dir is not None else _default_servers_dir()
    servers_path = servers_path.resolve()

    entries_by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    if not servers_path.exists():
        logger.info(f"MCP servers directory not found (skipping): {servers_path}")
        return McpDirectoryLoadResult(entries_by_id=entries_by_id, errors=errors)

    for path in sorted(servers_path.rglob("*.y*ml")):
        # Ignore docs files (we keep README.md in this directory)
        if path.name.lower().endswith(".md"):
            continue

        try:
            doc = _load_yaml(path)
        except Exception as e:
            errors.append(f"{path}: YAML parse error: {e}")
            continue

        if not isinstance(doc, dict):
            errors.append(f"{path}: Top-level YAML must be an object/mapping")
            continue

        # Prefer jsonschema if available, otherwise minimal validation.
        schema_err = _try_jsonschema_validate(doc)
        basic_err = _basic_validate_entry(doc)
        if schema_err:
            errors.append(f"{path}: {schema_err}")
            continue
        if basic_err:
            errors.append(f"{path}: {basic_err}")
            continue

        server_id = doc.get("server_id")
        if server_id in entries_by_id:
            errors.append(f"{path}: Duplicate server_id '{server_id}' (already defined elsewhere)")
            continue

        entries_by_id[str(server_id)] = doc

    if errors:
        logger.warning(f"MCP directory loaded with {len(errors)} issue(s).")
        for e in errors[:10]:
            logger.warning(f"MCP directory issue: {e}")

    logger.info(f"Loaded MCP server entries: {len(entries_by_id)} from {servers_path}")
    return McpDirectoryLoadResult(entries_by_id=entries_by_id, errors=errors)

