from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

try:
    from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator  # type: ignore
except Exception:  # pragma: no cover
    BaseModel = None  # type: ignore
    ConfigDict = None  # type: ignore
    Field = None  # type: ignore
    field_validator = None  # type: ignore
    model_validator = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVERS_DIR = REPO_ROOT / "mcp" / "servers"
SCHEMA_PATH = REPO_ROOT / "mcp" / "schemas" / "mcp_server_entry.schema.json"


if BaseModel is not None:
    class PolicyModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        default_requires_approval: bool
        max_concurrent_calls: int = Field(ge=1, le=1000)
        call_timeout_seconds: int = Field(ge=1, le=3600)
        max_result_bytes: int = Field(ge=1024, le=50_000_000)
        rate_limit_per_minute: Optional[int] = Field(default=None, ge=1)
        allow_network_access: Optional[bool] = None


    class LaunchOptionModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        id: str
        transport: str

        # stdio
        command: Optional[str] = None
        args: Optional[list[str]] = None
        env: Optional[dict[str, str]] = None

        # http
        url: Optional[str] = None
        headers: Optional[dict[str, str]] = None

        @field_validator("transport")
        @classmethod
        def _validate_transport(cls, v: str) -> str:
            allowed = {"stdio", "streamable_http", "http_sse_deprecated"}
            if v not in allowed:
                raise ValueError(f"transport must be one of {sorted(allowed)}")
            return v

        @model_validator(mode="after")
        def _validate_transport_requirements(self) -> "LaunchOptionModel":
            if self.transport == "stdio":
                if not self.command:
                    raise ValueError("launch_options[].command is required when transport == 'stdio'")
                if self.args is None:
                    raise ValueError("launch_options[].args is required when transport == 'stdio'")
            else:
                if not self.url:
                    raise ValueError("launch_options[].url is required when transport is HTTP-based")
            return self


    class McpServerEntryModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        schema_version: int = Field(ge=1)
        server_id: str
        display_name: str
        description: Optional[str] = None
        source: str
        enabled: bool

        tags: Optional[list[str]] = None
        transport_preferences: Optional[list[str]] = None

        launch_options: list[LaunchOptionModel]
        policy: PolicyModel

        tool_namespace: Optional[str] = None
        tool_allowlist: Optional[list[str]] = None
        tool_denylist: Optional[list[str]] = None

        @field_validator("server_id")
        @classmethod
        def _validate_server_id(cls, v: str) -> str:
            # Mirror the pattern from `mcp/schemas/mcp_server_entry.schema.json`
            import re

            pat = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+)*\/[a-zA-Z0-9._-]+$")
            if not pat.match(v):
                raise ValueError(
                    "server_id must look like '<namespace>/<name>', e.g. io.modelcontextprotocol/time or pypi/mcp-github"
                )
            return v

        @field_validator("source")
        @classmethod
        def _validate_source(cls, v: str) -> str:
            allowed = {
                "official_reference_server",
                "official_registry",
                "third_party_registry",
                "local_only",
            }
            if v not in allowed:
                raise ValueError(f"source must be one of {sorted(allowed)}")
            return v

        @field_validator("transport_preferences")
        @classmethod
        def _validate_transport_prefs(cls, v: Optional[list[str]]) -> Optional[list[str]]:
            if v is None:
                return None
            allowed = {"stdio", "streamable_http", "http_sse_deprecated"}
            bad = [x for x in v if x not in allowed]
            if bad:
                raise ValueError(f"transport_preferences contains invalid values: {bad}")
            return v


@dataclass(frozen=True)
class ValidationIssue:
    file: Path
    message: str


def _basic_validate_entry(doc: dict[str, Any]) -> Optional[str]:
    """
    Minimal validation with no optional deps beyond YAML parsing.

    This is intentionally conservative: it checks required keys and core shapes,
    but does not attempt full JSON Schema validation.
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

    # Validate policy required keys
    pol = doc["policy"]
    pol_required = {"default_requires_approval", "max_concurrent_calls", "call_timeout_seconds", "max_result_bytes"}
    pol_missing = [k for k in sorted(pol_required) if k not in pol]
    if pol_missing:
        return f"policy is missing required keys: {pol_missing}"

    # Validate each launch option minimally
    for i, opt in enumerate(doc["launch_options"]):
        if not isinstance(opt, dict):
            return f"launch_options[{i}] must be an object"
        if "id" not in opt or "transport" not in opt:
            return f"launch_options[{i}] must include 'id' and 'transport'"
        transport = opt.get("transport")
        if transport == "stdio":
            if not opt.get("command") or "args" not in opt:
                return f"launch_options[{i}] requires command+args for stdio transport"
        elif transport in ("streamable_http", "http_sse_deprecated"):
            if not opt.get("url"):
                return f"launch_options[{i}] requires url for HTTP transport"
        else:
            return f"launch_options[{i}].transport invalid: {transport!r}"

    return None


def _iter_server_entry_files(servers_dir: Path) -> Iterable[Path]:
    if not servers_dir.exists():
        return []
    return sorted(servers_dir.rglob("*.y*ml"))


def _load_yaml(path: Path) -> Any:
    if yaml is None:  # pragma: no cover
        raise RuntimeError("Missing dependency: PyYAML. Install requirements.txt / requirements_full.txt.")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validate_with_jsonschema(doc: Any) -> Optional[str]:
    """
    Returns an error string if invalid, or None if valid.

    This is optional (only used if `jsonschema` is installed). We keep a Pydantic
    fallback so `requirements.txt` / alpha installs can still validate entries.
    """
    try:
        import json
        from jsonschema import Draft202012Validator
    except Exception:
        return "__JSONSCHEMA_NOT_AVAILABLE__"

    if not SCHEMA_PATH.exists():
        return f"Schema file missing: {SCHEMA_PATH}"

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if not errors:
        return None

    # Show only first error to keep output readable (others usually cascade)
    e = errors[0]
    path = ".".join([str(p) for p in e.absolute_path]) or "<root>"
    return f"jsonschema validation failed at {path}: {e.message}"


def validate_all_server_entries(
    servers_dir: Path,
    *,
    use_jsonschema_if_available: bool = True,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for path in _iter_server_entry_files(servers_dir):
        try:
            doc = _load_yaml(path)
        except Exception as e:
            issues.append(ValidationIssue(path, f"YAML parse error: {e}"))
            continue

        if not isinstance(doc, dict):
            issues.append(ValidationIssue(path, f"Top-level YAML must be a mapping/object, got {type(doc)}"))
            continue

        if use_jsonschema_if_available:
            err = _validate_with_jsonschema(doc)
            if err and err != "__JSONSCHEMA_NOT_AVAILABLE__":
                issues.append(ValidationIssue(path, err))
                continue

        # Pydantic fallback (or secondary validation)
        if BaseModel is not None:
            try:
                McpServerEntryModel.model_validate(doc)  # type: ignore[name-defined]
            except Exception as e:
                issues.append(ValidationIssue(path, f"pydantic validation failed: {e}"))
                continue
        else:
            # Last-resort: minimal validation (works in very minimal environments).
            basic_err = _basic_validate_entry(doc)
            if basic_err:
                issues.append(ValidationIssue(path, f"basic validation failed: {basic_err}"))
                continue

    return issues


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate curated MCP server entries.")
    parser.add_argument(
        "--servers-dir",
        default=str(DEFAULT_SERVERS_DIR),
        help="Directory containing MCP server YAML entries (default: mcp/servers).",
    )
    parser.add_argument(
        "--no-jsonschema",
        action="store_true",
        help="Skip JSON Schema validation even if `jsonschema` is installed.",
    )

    args = parser.parse_args(argv)

    servers_dir = Path(args.servers_dir).resolve()

    issues = validate_all_server_entries(
        servers_dir=servers_dir,
        use_jsonschema_if_available=not args.no_jsonschema,
    )
    if not issues:
        print(f"OK: MCP server entries valid ({servers_dir})")
        return 0

    print(f"ERROR: MCP server entries invalid ({len(issues)} issue(s))")
    for issue in issues:
        rel = issue.file.resolve()
        try:
            rel = rel.relative_to(REPO_ROOT)
        except Exception:
            pass
        print(f"- {rel}: {issue.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

