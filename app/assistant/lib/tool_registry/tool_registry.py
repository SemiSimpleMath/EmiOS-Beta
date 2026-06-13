import copy
import json
from pathlib import Path
from importlib import util as import_util
from jinja2 import Environment, FileSystemLoader
from typing import Any


from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root
logger = get_logger(__name__)

from app.assistant.lib.tool_registry.mcp_server_directory import load_mcp_server_directory
from app.assistant.lib.tool_registry.mcp_tool_cache import load_mcp_tool_cache
from app.assistant.lib.tool_registry.mcp_install_registry import list_installed_records
from app.assistant.lib.tool_registry.mcp_trust_policy import is_trusted_mcp_server
from app.assistant.lib.tool_registry.mcp_schema_to_pydantic import build_mcp_tool_models


class ToolDescription(str):
    """
    String-compatible tool description with structured metadata attached.
    """

    def __new__(cls, value: str, *, tool_name: str, description: str, contract: dict[str, Any] | None):
        obj = str.__new__(cls, value or "")
        obj.tool_name = tool_name
        obj.description = description or ""
        obj.contract = contract if isinstance(contract, dict) else None
        return obj

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": getattr(self, "tool_name", ""),
            "description": getattr(self, "description", str(self)),
            "contract": getattr(self, "contract", None),
            "display": str(self),
        }


def get_tool_registry_dir():
    tool_dir = get_app_root() / "assistant" / "lib" / "tools"
    return str(tool_dir)

class ToolRegistry:
    def __init__(self, tools_dir=None):
        """
        Initialize the registry by scanning the given tools directory.
        Each subdirectory should be named after the tool and contain:
         - <tool_name>.py exposing `get_tool_class` — see patterns below
         - tool_forms/tool_forms.py (with a `<tool_name>_args` Pydantic model
           and `<tool_name>_arguments` wrapper)
         - prompts/<tool_name>_description.j2
         - tool_contract.json (`inputs[]` mirrors the Pydantic args fields;
           `arguments_prompt` carries planner-facing argument guidance)

        Three patterns coexist for `get_tool_class` (all valid):

         1. **Self-class tool** — defines its own class subclassing `BaseTool`
            and returns it. Class is CamelCase + `Tool` suffix
            (e.g. `class AppendTextFileTool(BaseTool)`). Most tools.

                def get_tool_class():
                    return AppendTextFileTool

         2. **Manager-as-tool** — wraps a multi-agent manager via
            `ManagerInterface`. Class name matches the dir name in
            snake_case (e.g. `class web_manager(BaseTool)`) so it lines up
            with the manager id everywhere. Used by all `*_manager/` tool
            dirs in this directory.

         3. **Shared-core adapter** — `<tool_name>.py` is a 3-line module
            that points `get_tool_class` at a shared core class via
            `create_tool_loader`. Used when multiple tools delegate to one
            core (e.g. `create_calendar_event`, `delete_calendar_event`,
            and `update_calendar_event` all share `CalendarTool`).

                from app.assistant.lib.core_tools.calendar_tool.calendar_tool import CalendarTool
                from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader
                get_tool_class = create_tool_loader(CalendarTool)
        """
        if tools_dir is None:
            tools_dir = get_tool_registry_dir()
        self.tools_dir = Path(tools_dir)
        self.registry = {}
        # MCP server directory entries (metadata only; not tools yet)
        self.mcp_server_entries = {}
        self.mcp_directory_errors = []
        self.mcp_tool_cache_errors = []

    def load_tool_registry(self):
        self.load_tools()

    def load_mcp_servers(self, servers_dir: str | None = None):
        """
        Load curated MCP server entries from `mcp/servers/`.

        This loads metadata only (no server processes are started, no network calls).
        """
        result = load_mcp_server_directory(servers_dir=servers_dir)
        trusted_entries: dict[str, dict[str, Any]] = {}
        for server_id, entry in (result.entries_by_id or {}).items():
            if not is_trusted_mcp_server(server_id):
                logger.warning("Skipping untrusted MCP server entry: %s", server_id)
                continue
            trusted_entries[server_id] = entry
        self.mcp_server_entries = trusted_entries
        self.mcp_directory_errors = result.errors
        if result.errors:
            logger.warning(f"MCP server directory loaded with {len(result.errors)} error(s).")
        else:
            logger.info(f"MCP server directory loaded ({len(self.mcp_server_entries)} trusted servers).")

    def load_mcp_tool_cache(self, enabled_only: bool = True):
        """
        Load cached MCP tools for curated servers and register them as ToolRegistry entries.

        This allows:
        - tool descriptions to be injected into prompts
        - tool argument schemas to be available (via generated Pydantic models)

        No MCP connections are made here; this reads `mcp/tool_cache/*.json`.
        """
        self.mcp_tool_cache_errors = []

        for server_id, entry in (self.mcp_server_entries or {}).items():
            if not is_trusted_mcp_server(server_id):
                self.mcp_tool_cache_errors.append(f"{server_id}: untrusted MCP server")
                continue
            if enabled_only and not bool(entry.get("enabled", False)):
                continue

            allowlist = entry.get("tool_allowlist") or []
            denylist = entry.get("tool_denylist") or []
            allowset = set(allowlist) if isinstance(allowlist, list) else set()
            denyset = set(denylist) if isinstance(denylist, list) else set()

            res = load_mcp_tool_cache(server_id)
            if res.errors:
                self.mcp_tool_cache_errors.extend([f"{server_id}: {e}" for e in res.errors])
                continue

            # If the server is enabled but no cache file exists yet, surface a clear warning.
            # (Otherwise we misleadingly log "MCP tool cache loaded successfully" even though
            # nothing was loaded for this enabled server.)
            try:
                if not res.cache_path.exists():
                    self.mcp_tool_cache_errors.append(
                        f"{server_id}: missing tool cache file (expected: {res.cache_path})"
                    )
                    continue
            except Exception:
                # If filesystem checks fail for some reason, fall back to old behavior.
                pass

            if not res.tools:
                continue

            for tool in res.tools:
                raw_tool_name = tool.get("name")
                if not isinstance(raw_tool_name, str) or not raw_tool_name:
                    continue
                if allowset and raw_tool_name not in allowset:
                    continue
                if denyset and raw_tool_name in denyset:
                    continue
                try:
                    self._register_mcp_cached_tool(server_id=server_id, tool=tool)
                except Exception as e:
                    self.mcp_tool_cache_errors.append(
                        f"{server_id}/{raw_tool_name}: failed to register cached MCP tool: {e}"
                    )
                    continue

        if self.mcp_tool_cache_errors:
            logger.warning(f"MCP tool cache loaded with {len(self.mcp_tool_cache_errors)} error(s).")
        else:
            logger.info("MCP tool cache loaded successfully.")

    def _register_mcp_cached_tool(self, *, server_id: str, tool: dict[str, Any]):
        raw_tool_name = tool.get("name")
        if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
            raise ValueError("MCP tool requires non-empty name.")
        description = tool.get("description") or ""
        input_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}

        namespaced = f"mcp::{server_id}::{raw_tool_name}"
        models = build_mcp_tool_models(
            namespaced_tool_name=namespaced,
            input_schema=input_schema,
        )

        # Auto-generate compact card from MCP schema for planner injection.
        # We intentionally do not require a checked-in tool_contract.json for MCP tools.
        contract_inputs: list[dict[str, Any]] = []
        if isinstance(input_schema.get("properties"), dict):
            req = set(input_schema.get("required") or []) if isinstance(input_schema.get("required"), list) else set()
            for arg_name, arg_schema in input_schema.get("properties", {}).items():
                if not isinstance(arg_name, str) or not arg_name.strip():
                    continue
                if not isinstance(arg_schema, dict):
                    continue
                typ = arg_schema.get("type")
                type_str = str(typ if isinstance(typ, str) else "any")
                desc = str(arg_schema.get("description") or "").strip()
                contract_inputs.append(
                    {
                        "name": arg_name.strip(),
                        "type": type_str,
                        "required": arg_name in req,
                        "description": desc,
                    }
                )
        generated_contract = {
            "name": namespaced,
            "description": str(description or "").strip(),
            "inputs": contract_inputs,
            "outputs": [
                {"path": "content", "type": "text", "description": "Human-readable tool result text."},
                {"path": "data.call_response", "type": "object", "description": "Raw MCP tools/call response (sanitized)."},
            ],
        }

        # Register as a tool entry (no local tool_class; ToolCaller executes through MCP adapter).
        self.registry[namespaced] = {
            "backend": "mcp",
            "tool_class": None,
            "tool_args": {"args": models.inner_args_model, "arguments": models.outer_arguments_model},
            "tool_form": models.inner_args_model,
            "prompts": {},
            "mcp_server_id": server_id,
            "mcp_tool_name": raw_tool_name,
            "mcp_input_schema": input_schema,
            "mcp_description": description,
            "mcp_annotations": tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {},
            "tool_contract": generated_contract,
        }

    def load_installed_mcp_tools(self, *, enabled_only: bool = True):
        """
        Register only tools that were explicitly installed via the MCP installer flow.
        """
        records = list_installed_records(enabled_only=enabled_only)
        if not records:
            logger.info("No installed MCP tools found in install registry.")
            return

        by_server: dict[str, set[str]] = {}
        for rec in records:
            by_server.setdefault(rec.server_id, set()).add(rec.tool_name)

        for server_id, tool_names in by_server.items():
            if not is_trusted_mcp_server(server_id):
                self.mcp_tool_cache_errors.append(f"{server_id}: installed tool belongs to untrusted MCP server")
                continue
            res = load_mcp_tool_cache(server_id)
            if res.errors:
                for err in res.errors:
                    self.mcp_tool_cache_errors.append(f"{server_id}: {err}")
                continue
            for tool in res.tools:
                name = tool.get("name")
                if not isinstance(name, str) or name not in tool_names:
                    continue
                try:
                    self._register_mcp_cached_tool(server_id=server_id, tool=tool)
                except Exception as e:
                    self.mcp_tool_cache_errors.append(
                        f"{server_id}/{name}: failed to register installed MCP tool: {e}"
                    )

    def list_mcp_servers(self, enabled_only: bool = False) -> list[str]:
        servers = list(self.mcp_server_entries.keys())
        if not enabled_only:
            return servers
        enabled = []
        for sid, entry in self.mcp_server_entries.items():
            if bool(entry.get("enabled", False)):
                enabled.append(sid)
        return enabled

    def get_mcp_server_entry(self, server_id: str):
        return self.mcp_server_entries.get(server_id)

    def load_tools(self):
        failures: list[str] = []
        for tool_dir in self.tools_dir.iterdir():
            if tool_dir.is_dir():
                tool_name = tool_dir.name
                # Ignore non-tool directories.
                if tool_name.startswith("__"):
                    continue
                
                # Skip tools that have a .disabled file in their directory
                disabled_marker = tool_dir / ".disabled"
                if disabled_marker.exists():
                    logger.info(f"Skipping disabled tool: {tool_name}")
                    continue

                # A valid tool directory must contain <tool_name>.py.
                # If missing (e.g., stale/empty folder), skip silently.
                tool_file = tool_dir / f"{tool_name}.py"
                if not tool_file.exists():
                    logger.debug("Skipping non-tool directory '%s' (missing %s).", tool_name, tool_file.name)
                    continue
                
                try:
                    tool_class = self.load_tool_class(tool_dir, tool_name)
                    tool_args = self.load_tool_args(tool_dir, tool_name)
                    prompts = self.load_prompts(tool_dir, tool_name)
                    tool_contract = self.load_tool_contract(tool_dir, tool_name)
                    self.registry[tool_name] = {
                        "tool_class": tool_class,
                        "tool_args": tool_args,
                        "prompts": prompts,
                        "tool_contract": tool_contract,
                    }
                    logger.info(f"Registered tool: {tool_name}")
                except Exception as e:
                    logger.error(f"Failed to load tool '{tool_name}': {e}")
                    failures.append(f"{tool_name}: {e}")
        if failures:
            # Fail loud: a core tool that won't load (bad import, or a malformed
            # tool_contract — including the min_authority / approval_min_authority
            # range checks) must NOT silently vanish from the registry while boot
            # reports success. A missing security-gated tool is a defect to fix,
            # not to skip past. (MCP tool-cache errors are collected separately;
            # this guards the first-party tools in tools_dir.)
            raise RuntimeError(
                f"Tool registry failed to load {len(failures)} tool(s); refusing to "
                f"start with a partial registry:\n  - " + "\n  - ".join(failures)
            )

    def load_tool_class(self, tool_dir: Path, tool_name: str):
        tool_file = tool_dir / f"{tool_name}.py"
        if not tool_file.exists():
            raise FileNotFoundError(f"{tool_file} does not exist.")
        module_name = f"{tool_name}_tool"
        spec = import_util.spec_from_file_location(module_name, str(tool_file))
        module = import_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "get_tool_class"):
            raise AttributeError(f"Module {module_name} must define get_tool_class().")
        return module.get_tool_class()

    def load_tool_args(self, tool_dir: Path, tool_name: str):
        forms_dir = tool_dir / "tool_forms"
        if not forms_dir.exists():
            raise FileNotFoundError(f"Directory {forms_dir} not found for tool '{tool_name}'.")
        form_file = forms_dir / "tool_forms.py"
        if not form_file.exists():
            raise FileNotFoundError(f"{form_file} not found for tool '{tool_name}'.")
        module_name = f"{tool_name}_form"
        spec = import_util.spec_from_file_location(module_name, str(form_file))
        module = import_util.module_from_spec(spec)
        spec.loader.exec_module(module)

        args_class_name = f"{tool_name}_args"
        arguments_class_name = f"{tool_name}_arguments"

        if not hasattr(module, args_class_name):
            raise ValueError(f"{args_class_name} not found in {form_file}.")
        if not hasattr(module, arguments_class_name):
            raise ValueError(f"{arguments_class_name} not found in {form_file}.")

        args_class = getattr(module, args_class_name)
        arguments_class = getattr(module, arguments_class_name)

        from pydantic import BaseModel
        if not (issubclass(args_class, BaseModel) and issubclass(arguments_class, BaseModel)):
            raise TypeError("Both classes must be subclasses of BaseModel.")

        return {"args": args_class, "arguments": arguments_class}

    def load_prompts(self, tool_dir: Path, tool_name: str):
        prompts_dir = tool_dir / "prompts"
        if not prompts_dir.exists():
            raise FileNotFoundError(f"Prompts directory not found in {tool_dir}.")
        env = Environment(loader=FileSystemLoader(str(prompts_dir)))
        prompts = {}
        # Only the description prompt is loaded as a Jinja template now.
        # Argument guidance (formerly <name>_args.j2) lives in the JSON
        # contract's `arguments_prompt` field; we serve that string directly.
        prompt = f"{tool_name}_description.j2"
        key = prompt.split(".")[0]
        try:
            template = env.get_template(prompt)
            prompts[key] = template
        except Exception as e:
            logger.error(f"Failed to load prompt '{prompt}' for tool '{tool_dir.name}': {e}")
            prompts[key] = None
        return prompts

    def load_tool_contract(self, tool_dir: Path, tool_name: str):
        """
        Load an optional per-tool contract from tool_contract.json.
        Missing/invalid contracts are non-fatal.
        """
        contract_file = tool_dir / "tool_contract.json"
        if not contract_file.exists():
            return None
        try:
            with open(contract_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logger.warning(
                    "Ignoring non-object tool contract for '%s' at %s",
                    tool_name,
                    str(contract_file),
                )
                return None
            return self._normalize_tool_contract(raw, tool_name)
        except Exception as e:
            logger.warning(
                "Failed to load tool contract for '%s' at %s: %s",
                tool_name,
                str(contract_file),
                e,
            )
            return None

    def _normalize_tool_contract(self, raw: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
        name = raw.get("name")
        description = raw.get("description")
        inputs = raw.get("inputs")
        outputs = raw.get("outputs")
        arguments_prompt = raw.get("arguments_prompt")
        metadata = raw.get("metadata")

        normalized: dict[str, Any] = {
            "name": str(name).strip() if isinstance(name, str) and name.strip() else tool_name,
            "description": str(description).strip() if isinstance(description, str) else "",
            "inputs": [],
            "outputs": [],
        }

        if isinstance(inputs, list):
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                in_name = item.get("name")
                in_type = item.get("type")
                if not isinstance(in_name, str) or not in_name.strip():
                    continue
                if not isinstance(in_type, str) or not in_type.strip():
                    continue
                normalized["inputs"].append(
                    {
                        "name": in_name.strip(),
                        "type": in_type.strip(),
                        "required": bool(item.get("required", False)),
                        "description": str(item.get("description") or "").strip(),
                    }
                )

        if isinstance(outputs, list):
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                out_path = item.get("path")
                out_type = item.get("type")
                if not isinstance(out_path, str) or not out_path.strip():
                    continue
                if not isinstance(out_type, str) or not out_type.strip():
                    continue
                normalized["outputs"].append(
                    {
                        "path": out_path.strip(),
                        "type": out_type.strip(),
                        "description": str(item.get("description") or "").strip(),
                    }
                )

        if isinstance(arguments_prompt, str) and arguments_prompt.strip():
            normalized["arguments_prompt"] = arguments_prompt.strip()
        normalized["metadata"] = self._normalize_tool_metadata(metadata)
        return normalized

    def _normalize_tool_metadata(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raw = {}
        # New schema: domain / actions / selectors. Fall back to legacy
        # category / verbs / entities for contracts that haven't migrated yet.
        domain = str(raw.get("domain") or raw.get("category") or "general").strip().lower() or "general"
        actions_raw = raw.get("actions") if raw.get("actions") is not None else raw.get("verbs")
        selectors_raw = raw.get("selectors") if raw.get("selectors") is not None else raw.get("entities")

        def _str_list(val: Any) -> list[str]:
            if isinstance(val, list):
                return [str(x).strip().lower() for x in val if isinstance(x, str) and str(x).strip()]
            return []

        actions = _str_list(actions_raw)
        selectors = _str_list(selectors_raw)

        risk_level = str(raw.get("risk_level") or "low").strip().lower() or "low"
        side_effects = str(raw.get("side_effects") or "read_only").strip().lower() or "read_only"
        cost_level = str(raw.get("cost_level") or "low").strip().lower() or "low"
        latency_class = str(raw.get("latency_class") or "fast").strip().lower() or "fast"
        room_visibility_default = str(raw.get("room_visibility_default") or "conditional").strip().lower() or "conditional"
        requires_auth_raw = raw.get("requires_auth")
        requires_network_raw = raw.get("requires_network")
        front_door_raw = raw.get("front_door")
        approval_required_raw = raw.get("approval_required")
        approval_min_authority_raw = raw.get("approval_min_authority")
        min_authority_raw = raw.get("min_authority")
        planner_description = str(raw.get("planner_description") or "").strip()

        requires_auth: list[str] = []
        if isinstance(requires_auth_raw, list):
            requires_auth = [str(x).strip().lower() for x in requires_auth_raw if isinstance(x, str) and str(x).strip()]
        elif isinstance(requires_auth_raw, str) and requires_auth_raw.strip():
            requires_auth = [requires_auth_raw.strip().lower()]

        approval_min_authority: int | None = None
        if "approval_min_authority" in raw and approval_min_authority_raw is not None:
            if isinstance(approval_min_authority_raw, bool):
                raise ValueError("tool_contract.metadata.approval_min_authority must be an integer between 0 and 100.")
            try:
                parsed_level = int(approval_min_authority_raw)
            except Exception:
                raise ValueError("tool_contract.metadata.approval_min_authority must be an integer between 0 and 100.")
            if parsed_level < 0 or parsed_level > 100:
                raise ValueError("tool_contract.metadata.approval_min_authority must be between 0 and 100.")
            approval_min_authority = parsed_level

        # L1 floor (min_authority) — the see+use gate. Mirror approval_min_authority's
        # parse/validate. This MUST be preserved here: it lives in the contract JSON,
        # but if it's dropped in normalization, resolve_tool_min_authority hits its
        # fail-closed 99 default for every tool and denies all sub-100 scopes.
        min_authority: int | None = None
        if "min_authority" in raw and min_authority_raw is not None:
            if isinstance(min_authority_raw, bool):
                raise ValueError("tool_contract.metadata.min_authority must be an integer between 0 and 100.")
            try:
                parsed_floor = int(min_authority_raw)
            except Exception:
                raise ValueError("tool_contract.metadata.min_authority must be an integer between 0 and 100.")
            if parsed_floor < 0 or parsed_floor > 100:
                raise ValueError("tool_contract.metadata.min_authority must be between 0 and 100.")
            min_authority = parsed_floor

        return {
            "domain": domain,
            "actions": actions,
            "selectors": selectors,
            "planner_description": planner_description,
            # Legacy aliases for any callers still reading the old names.
            "category": domain,
            "verbs": actions,
            "entities": selectors,
            "risk_level": risk_level,
            "side_effects": side_effects,
            "requires_auth": requires_auth,
            "requires_network": bool(requires_network_raw),
            "cost_level": cost_level,
            "latency_class": latency_class,
            "front_door": bool(front_door_raw),
            "room_visibility_default": room_visibility_default,
            "approval_required": bool(approval_required_raw),
            "approval_min_authority": approval_min_authority,
            "min_authority": min_authority,
        }


    def get_tool(self, tool_name: str):
        """
        Retrieves the configuration for a specified tool.
        """
        tool_config = self.registry.get(tool_name)
        if not tool_config:
            logger.debug(f"Tool '{tool_name}' not found in registry.")
            return None

        # Ensure correct structure for tool_form.
        # Local tools use the outer `<tool>_arguments` form; MCP tools should use the inner
        # MCP arguments model so ToolArguments generates the correct shape.
        if tool_config.get("backend") == "mcp":
            return {**tool_config, "tool_form": tool_config.get("tool_form") or tool_config["tool_args"]["args"]}
        return {**tool_config, "tool_form": tool_config["tool_args"]["arguments"]}

    def list_tools(self):
        return list(self.registry.keys())


    def get_all_tools(self):
        """
        Returns a copy of the entire tool registry.

        Returns:
        - Dictionary containing all registered tools.
        """
        return self.registry.copy()

    def get_tool_description(self, tool_name: str):
        """Retrieve and render the tool description template."""
        tool = self.registry.get(tool_name)
        if not tool:
            logger.debug(f"Tool '{tool_name}' not found in registry.")
            return None

        # MCP tools store their description directly (no Jinja templates).
        if tool.get("backend") == "mcp":
            desc = str(tool.get("mcp_description") or "No description available.")
            return ToolDescription(
                desc,
                tool_name=tool_name,
                description=desc,
                contract=None,
            )

        description_template = tool["prompts"].get(f"{tool_name}_description")
        desc_text = "No description available."
        if description_template:
            try:
                desc_text = description_template.render()
            except Exception as e:
                logger.error(f"Error rendering description for '{tool_name}': {e}")
                desc_text = "Error rendering description."
        contract = tool.get("tool_contract") if isinstance(tool.get("tool_contract"), dict) else None
        devices_block = self._smart_home_devices_block(tool_name)
        display = self._format_description_with_contract(desc_text, contract, devices_block)
        return ToolDescription(
            display,
            tool_name=tool_name,
            description=desc_text,
            contract=contract,
        )

    def _smart_home_devices_block(self, tool_name: str) -> str:
        """Return a planner-facing summary of configured devices for a smart-home tool.

        Reads from configs/smart_home_tools.json. Empty string for non-smart-home
        tools or when the integration has no devices configured (default).
        """
        # Map tool name → integration key in smart_home_tools.json.
        _SH_TOOL_TO_INTEGRATION = {
            "lights_control": "lights",
            "ring_camera_control": "ring",
            "nest_home_control": "nest",
            "local_camera_snapshot": "local_cameras",
        }
        integration = _SH_TOOL_TO_INTEGRATION.get(tool_name)
        if integration is None:
            return ""
        try:
            from app.assistant.utils.path_utils import get_configs_dir
            import json
            cfg_path = get_configs_dir() / "smart_home_tools.json"
            if not cfg_path.exists():
                return ""
            payload = json.loads(cfg_path.read_text(encoding="utf-8"))
            devices = (payload.get(integration) or {}).get("devices") or []
            if not isinstance(devices, list) or not devices:
                return ""
            lines = []
            for d in devices:
                if not isinstance(d, dict):
                    continue
                alias = str(d.get("alias") or "").strip()
                if not alias:
                    continue
                # Show alias + any non-empty notes for context.
                notes = str(d.get("notes") or "").strip()
                kind = str(d.get("kind") or "").strip()
                # Identifying info varies per integration; show whatever non-alias
                # field is present so the user can sanity-check the listing.
                # Note: rtsp_url is intentionally omitted (contains credentials).
                ident_fields = [
                    f"kind={kind}" if kind else "",
                    f"host={d.get('host')}" if d.get("host") else "",
                    f"camera_id={d.get('camera_id')}" if d.get("camera_id") else "",
                    f"device_id={d.get('device_id')}" if d.get("device_id") else "",
                ]
                ident = ", ".join([f for f in ident_fields if f])
                trailer = " — ".join([t for t in [notes, ident] if t])
                lines.append(f"  - {alias}" + (f" ({trailer})" if trailer else ""))
            if not lines:
                return ""
            return "Configured devices (use these names exactly):\n" + "\n".join(lines)
        except Exception as e:
            logger.error("Failed to render smart-home devices for '%s': %s", tool_name, e)
            return ""

    # Outputs are auto-rendered only when meaningfully tool-specific.
    # Every tool emits `content: text` + `data: object` — showing that in the
    # planner system prompt 15× per call costs ~750 chars of pure boilerplate.
    # `args_text` (the contract's `arguments_prompt`) is INTENTIONALLY not
    # rendered here. Planners pick the tool from description + Inputs; the
    # detailed argument guidance reaches the args agent via
    # `get_tool_arguments_prompt()` when fast_validate fails.
    _BOILERPLATE_OUTPUT_PATHS = frozenset({"content", "data"})

    def _outputs_are_boilerplate(self, outputs: list) -> bool:
        if not outputs:
            return True
        for item in outputs:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if path and path not in self._BOILERPLATE_OUTPUT_PATHS:
                return False
        return True

    def _format_description_with_contract(
        self,
        desc_text: str,
        contract: dict[str, Any] | None,
        devices_block: str = "",
    ) -> str:
        base = str(desc_text or "").strip() or "No description available."
        devices = str(devices_block or "").strip()
        if not isinstance(contract, dict):
            if devices:
                return base + "\n\n" + devices
            return base
        lines: list[str] = [base, "Contract:"]
        inputs = contract.get("inputs") if isinstance(contract.get("inputs"), list) else []
        if inputs:
            lines.append("  Inputs:")
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                typ = str(item.get("type") or "").strip()
                if not name or not typ:
                    continue
                req = " required" if bool(item.get("required", False)) else ""
                lines.append(f"    - {name}: {typ}{req}")
        outputs = contract.get("outputs") if isinstance(contract.get("outputs"), list) else []
        if outputs and not self._outputs_are_boilerplate(outputs):
            lines.append("  Outputs:")
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                typ = str(item.get("type") or "").strip()
                if not path or not typ:
                    continue
                lines.append(f"    - {path}: {typ}")
        if devices:
            lines.append(devices)
        return "\n".join(lines).strip()

    def _format_compact_tool_card(
        self,
        *,
        tool_name: str,
        description: str,
        contract: dict[str, Any] | None,
    ) -> str:
        """
        Compact planner-facing tool card.

        One-line capability hint plus bare argument names. Detailed arg
        descriptions / examples / output schemas are reserved for ToolArguments
        when the tool is actually invoked.

        Priority for the capability line:
          1. metadata.planner_description (explicit short hint in the contract)
          2. first sentence of description (capped)
          3. "No description available."
        """
        meta = contract.get("metadata") if isinstance(contract, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        short = str(meta.get("planner_description") or "").strip()
        if not short:
            raw = str(description or "").strip()
            if raw:
                # First sentence, capped at 140 chars.
                first_break = len(raw)
                for sep in (". ", "\n"):
                    idx = raw.find(sep)
                    if idx > 0 and idx < first_break:
                        first_break = idx
                short = raw[:first_break].strip()
                if len(short) > 140:
                    short = short[:137].rstrip() + "..."
        if not short:
            short = "No description available."

        inputs = (
            contract.get("inputs")
            if isinstance(contract, dict) and isinstance(contract.get("inputs"), list)
            else []
        )
        if not inputs:
            return short

        required: list[str] = []
        optional: list[str] = []
        for item in inputs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if bool(item.get("required", False)):
                required.append(name)
            else:
                optional.append(name)

        parts = [short]
        if required or optional:
            args_line = "args:"
            if required:
                args_line += " " + ", ".join(required)
            if optional:
                args_line += " [" + ", ".join(optional) + "]"
            parts.append(args_line)
        return " — ".join(parts)

    def get_tool_description_compact(self, tool_name: str):
        tool = self.registry.get(tool_name)
        if not tool:
            logger.debug(f"Tool '{tool_name}' not found in registry.")
            return None

        if tool.get("backend") == "mcp":
            desc = str(tool.get("mcp_description") or "No description available.")
            display = self._format_compact_tool_card(
                tool_name=tool_name,
                description=desc,
                contract=None,
            )
            return ToolDescription(
                display,
                tool_name=tool_name,
                description=desc,
                contract=None,
            )

        description_template = tool["prompts"].get(f"{tool_name}_description")
        desc_text = "No description available."
        if description_template:
            try:
                desc_text = description_template.render()
            except Exception as e:
                logger.error(f"Error rendering description for '{tool_name}': {e}")
                desc_text = "Error rendering description."
        contract = tool.get("tool_contract") if isinstance(tool.get("tool_contract"), dict) else None
        display = self._format_compact_tool_card(
            tool_name=tool_name,
            description=desc_text,
            contract=contract,
        )
        return ToolDescription(
            display,
            tool_name=tool_name,
            description=desc_text,
            contract=contract,
        )


    def get_tool_arguments_prompt(self, tool_name: str, user_context: dict = None):
        """Return the tool's argument-fill guidance string for the tool_args
        agent or planner. Sourced from the JSON contract's `arguments_prompt`
        field (formerly a Jinja-rendered <name>_args.j2 file)."""
        tool = self.registry.get(tool_name)
        if not tool:
            logger.debug(f"Tool '{tool_name}' not found in registry.")
            return None

        # MCP tools: generate a generic args prompt from the cached schema.
        if tool.get("backend") == "mcp":
            schema = tool.get("mcp_input_schema") or {}
            try:
                schema_str = json.dumps(schema, indent=2, ensure_ascii=True)
            except Exception:
                schema_str = str(schema)
            return (
                "Fill the tool call arguments according to this JSON Schema. "
                "Return ONLY the arguments object that matches the schema.\n\n"
                f"{schema_str}"
            )

        contract = tool.get("tool_contract") if isinstance(tool.get("tool_contract"), dict) else None
        if isinstance(contract, dict):
            ap = contract.get("arguments_prompt")
            if isinstance(ap, str) and ap.strip():
                return ap
        return "No arguments prompt available."


    def get_tool_descriptions(self, allowed_tools: list) -> dict:
        """Retrieve descriptions only for allowed tools.

        Planner-facing render: description.j2 + Contract Inputs (+ tool-specific
        Outputs when non-boilerplate). The contract's `arguments_prompt` is NOT
        included — it's args-agent territory, reachable via
        `get_tool_arguments_prompt()`. Boilerplate `content/data` outputs are
        suppressed; only tool-specific Outputs render. The compact path is
        still available via get_tool_description_compact() for callers that
        explicitly want a one-line summary.
        """
        return {tool: self.get_tool_description(tool) for tool in allowed_tools}

    def get_tool_contract(self, tool_name: str):
        tool = self.registry.get(tool_name)
        if not isinstance(tool, dict):
            return None
        contract = tool.get("tool_contract")
        return contract if isinstance(contract, dict) else None


    def get_tool_arguments_prompts(self, allowed_tools: list, user_context: dict = None) -> dict:
        """Retrieve argument prompts only for allowed tools."""
        return {tool: self.get_tool_arguments_prompt(tool, user_context) for tool in allowed_tools}

    def get_tool_form(self, tool_name: str):
        """Retrieve the tool form (arguments schema) for a specified tool."""
        tool_config = self.get_tool(tool_name)
        if not tool_config:
            logger.debug(f"Tool '{tool_name}' not found in registry.")
            return None
        return tool_config.get("tool_form")

    def get_tool_class(self, tool_name: str):
        tool_config = self.get_tool(tool_name)
        if not tool_config:
            logger.debug(f"Tool '{tool_name}' not found in registry.")
            return None
        return tool_config.get("tool_class")


    def filter_tools(self, allowed_tools):
        """
        Returns a filtered registry containing only the allowed tools.
        This avoids reinitializing the ToolRegistry.
        """
        filtered_registry = copy.copy(self)  # Shallow copy to avoid reinitialization
        filtered_registry.registry = {
            tool: self.registry[tool] for tool in allowed_tools if tool in self.registry
        }
        return filtered_registry



if __name__ == "__main__":
    tool_dir = Path(get_tool_registry_dir())
    print("Tool directory:", tool_dir)

    # Initialize and test the ToolRegistry
    registry = ToolRegistry(str(tool_dir))

    # List all tool names
    tool_list = registry.list_tools()
    print("List of tools:", tool_list)

    # Print the full registry (copy)
    full_registry = registry.get_all_tools()
    print("Full registry:", full_registry)

    # Specify a tool to test
    tool_name = "get_email"
    tool_config = registry.get_tool(tool_name)
    if not tool_config:
        print(f"Error: Tool '{tool_name}' not found in registry.")
    else:
        print(f"\nTool configuration for '{tool_name}':")
        print(tool_config)

        # Get and print tool description
        description = registry.get_tool_description(tool_name)
        print(f"\nDescription for '{tool_name}':")
        print(description)


        # Simulate a user_context dictionary and get tool arguments prompt
        user_context = {"user": "test_user", "data": "sample"}
        arguments_prompt = registry.get_tool_arguments_prompt(tool_name, user_context)
        print(f"\nArguments prompt for '{tool_name}':")
        print(arguments_prompt)

        # Get and print the tool form (arguments schema)
        tool_form = registry.get_tool_form(tool_name)
        print(f"\nTool form for '{tool_name}':")
        print(tool_form)
