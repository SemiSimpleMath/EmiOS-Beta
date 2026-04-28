import re
from pathlib import Path
import yaml
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

def validate_all(agent_registry: AgentRegistry):
    logger.info("Running agent registry validation...")

    _check_namespace_consistency()
    _check_prompt_integrity(agent_registry)
    _check_context_usage(agent_registry)
    _check_llm_params_contract(agent_registry)
    _check_manager_configs(set(agent_registry.list_agents()), agent_registry)

    logger.info("Agent system validation complete.")

def _check_namespace_consistency():
    base_dir = Path("app/assistant/agents")
    seen = set()
    for folder in base_dir.rglob("config.yaml"):
        # Skip archived agents
        if ".archive" in str(folder):
            continue
        # Skip agents marked .ignore (same rule as AgentRegistry)
        if (folder.parent / ".ignore").exists():
            continue
            
        rel = folder.parent.relative_to(base_dir)
        namespace = str(rel.parent).replace("/", "_") if rel.parent != Path(".") else None
        with open(folder) as f:
            config = yaml.safe_load(f)

        if not isinstance(config, dict):
            raise RuntimeError(f"❌ Invalid or empty config in {folder}. Got: {config}")

        name = config.get("name")
        expected = f"{namespace}::{name}" if namespace else name
        if expected in seen:
            raise RuntimeError(f"❌ Duplicate agent name: {expected}")
        seen.add(expected)

def _check_prompt_integrity(registry: AgentRegistry):
    for agent_name, config in registry.configs.items():
        if config.get("type") == "control_node":
            continue
        if config.get("class_name") == "Delegator":
            continue

        prompt_dir = Path("app/assistant/agents") / agent_name.replace("::", "/") / "prompts"
        if not (prompt_dir / "system.j2").exists():
            logger.warning(f"{agent_name} is missing system.j2")
        if not (prompt_dir / "user.j2").exists():
            logger.warning(f"{agent_name} is missing user.j2")
        # Note: description.j2 is only needed for agents that are called by other agents (e.g., shared::writer)
        # Most workflow agents don't need descriptions, so we don't warn about missing description.j2

def _check_context_usage(registry: AgentRegistry):
    # Map of sub-components to their parent components
    # These are declared separately in config but injected as part of their parent
    SUB_COMPONENT_MAP = {
        'entity_summary': 'entity_info',
        'entity_metadata': 'entity_info',
        # Add more mappings here as needed
    }
    
    for agent_name, config in registry.configs.items():
        # Skip control nodes - they don't have prompts
        if config.get("type") == "control_node":
            continue
            
        prompts = config.get("prompts", {})

        def check_usage(context_key: str, prompt_text: str, prompt_name: str):
            loaded_from = config.get("_loaded_from", "unknown")

            if prompt_text is None:
                raise RuntimeError(
                    f"❌ {agent_name}: {prompt_name}.j2 prompt text is None (failed to load?). "
                    f"Loaded from: {loaded_from}"
                )

            # Strict validation: missing key is fine (treated as empty list), but explicit null is an error.
            if context_key in config and config.get(context_key) is None:
                raise RuntimeError(
                    f"❌ {agent_name}: {context_key} is null in config.yaml. "
                    f"Use an explicit empty list: `{context_key}: []`. "
                    f"Loaded from: {loaded_from}"
                )

            fields = config.get(context_key, [])

            if not isinstance(fields, list):
                raise RuntimeError(
                    f"❌ {agent_name}: {context_key} must be a list, got {type(fields).__name__}: {fields}. "
                    f"Loaded from: {loaded_from}"
                )

            for field in fields:
                # Check if field is a sub-component
                parent_field = SUB_COMPONENT_MAP.get(field)
                
                if parent_field:
                    if parent_field not in prompt_text:
                        logger.warning(
                            f"{agent_name}: {context_key} declares sub-component '{field}', "
                            f"but parent '{parent_field}' is not found in {prompt_name}.j2"
                        )
                else:
                    if field not in prompt_text:
                        logger.warning(
                            f"{agent_name}: {context_key} declares '{field}', but it's not found in {prompt_name}.j2"
                        )

        if "system" in prompts:
            check_usage("system_context_items", prompts["system"], "system")

        if "user" in prompts:
            check_usage("user_context_items", prompts["user"], "user")


def _check_manager_configs(agent_names, registry):
    managers_dir = Path("app/assistant/multi_agents")
    for path in managers_dir.rglob("*.yaml"):
        if ".archive" in str(path):
            continue
        with open(path) as f:
            config = yaml.safe_load(f)

        used_agents = {a["name"] for a in config.get("agents", [])}
        unknown = used_agents - agent_names
        if unknown:
            raise RuntimeError(f"❌ {path} {path.name}: references unknown agents: {unknown}")

        state_map = config.get("flow_config", {}).get("state_map", {})
        flow_keys = set(state_map.keys())
        flow_targets = set(state_map.values())
        all_flow = flow_keys | flow_targets

        # Also count agents referenced in control_node configs (summary_agent, critic_agent, etc.)
        for node_entry in config.get("control_nodes", []):
            if not isinstance(node_entry, dict):
                continue
            node_cfg = node_entry.get("config")
            if isinstance(node_cfg, dict):
                for val in node_cfg.values():
                    if isinstance(val, str):
                        all_flow.add(val)
        # And agents referenced in flow_config sub-sections (summary, critic, etc.)
        flow_config = config.get("flow_config", {})
        for section_key, section in flow_config.items():
            if section_key == "state_map" or not isinstance(section, dict):
                continue
            for val in section.values():
                if isinstance(val, str):
                    all_flow.add(val)

        # Agents used as allowed_nodes by other agents are reachable via tool_caller.
        for agent_entry in config.get("agents", []):
            if not isinstance(agent_entry, dict):
                continue
            agent_name = agent_entry.get("name")
            if not isinstance(agent_name, str):
                continue
            agent_cfg = registry.get_agent_config(agent_name) if agent_name in agent_names else None
            if isinstance(agent_cfg, dict):
                for node in agent_cfg.get("allowed_nodes", []):
                    if isinstance(node, str):
                        all_flow.add(node)

        unused = used_agents - all_flow
        if unused:
            logger.warning(f"{path.name}: declared agents not used in flow_config: {unused}")


def _check_llm_params_contract(registry: AgentRegistry):
    """
    Enforce canonical LLM config shape.

    Rules:
    - Legacy top-level keys (`model`, `engine`, `temperature`, `timeout`) are forbidden.
    - If `llm_params` exists, it must be a dict.
    - `llm_params.model` is forbidden (use `llm_params.engine`).
    - If either `llm_provider` or `engine` is present in llm_params, both are required.
    """
    legacy_top_level_keys = {"model", "engine", "temperature", "timeout", "llm_provider"}
    violations: list[str] = []
    advisories: list[str] = []

    for agent_name, config in (registry.configs or {}).items():
        if not isinstance(config, dict):
            continue
        if config.get("type") == "control_node":
            continue

        for key in legacy_top_level_keys:
            if key in config:
                violations.append(
                    f"{agent_name}: top-level '{key}' is not supported; move it under 'llm_params.{key}'"
                )

        if "llm_params" not in config:
            continue

        llm_params = config.get("llm_params")
        if llm_params is None:
            violations.append(f"{agent_name}: 'llm_params' is null; use an explicit dict or omit it")
            continue
        if not isinstance(llm_params, dict):
            violations.append(
                f"{agent_name}: 'llm_params' must be a dict, got {type(llm_params).__name__}"
            )
            continue

        if "model" in llm_params:
            violations.append(
                f"{agent_name}: 'llm_params.model' is not supported; use 'llm_params.engine'"
            )

        has_provider = bool(llm_params.get("llm_provider"))
        has_engine = bool(llm_params.get("engine"))
        if has_provider != has_engine:
            violations.append(
                f"{agent_name}: llm_params must define both 'llm_provider' and 'engine' together"
            )

        engine = llm_params.get("engine")
        if isinstance(engine, str) and engine.strip().lower().startswith("gpt-5"):
            if "temperature" in llm_params:
                advisories.append(
                    f"{agent_name}: GPT-5 model '{engine}' should omit llm_params.temperature"
                )

    if violations:
        joined = "\n - ".join(violations)
        raise RuntimeError(f"❌ LLM params contract violations:\n - {joined}")
    if advisories:
        for item in advisories:
            logger.warning(item)
