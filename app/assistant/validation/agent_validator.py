import re
from pathlib import Path
import yaml
from jinja2 import Environment, meta as jinja_meta, nodes as jinja_nodes
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Framework-provided template vars that do NOT need to be declared in
# user_context_items / system_context_items. These are auto-injected by
# the prompt builder or the agent runtime regardless of agent config.
#
# Anchor: app/assistant/agent_runtime/services/context_injector.py
# in generate_injections_block. The dict literal there is the authority —
# any key always-added there (or conditionally always-added when the
# templated path would resolve) is a builtin and templates can rely on it.
_FRAMEWORK_BUILTINS = {
    # Loop / control-flow locals — Jinja's meta module excludes loop vars
    # from undeclared_variables already, but list these as a belt-and-braces
    # safety net for anything weird in Jinja's parser.
    "loop", "super", "self", "varargs", "kwargs",
    # context_injector.generate_injections_block always-populates these
    # keys onto the render context regardless of agent config:
    "date_time",
    "day_of_week",
    "action_count",
    "room_contact_name",
    "current_speaker_name",
    "skills",
    "auto_injected_skill_names",
    # Conditionally populated by context_injector — agents that don't
    # need them just get empty values, but they're never Undefined:
    "_keyword_injected_resources",  # keyword_resource_injection path
    "entity_info",                  # set when entity_keys present
}


def validate_all(agent_registry: AgentRegistry):
    logger.info("Running agent registry validation...")

    _check_namespace_consistency()
    _check_prompt_integrity(agent_registry)
    _check_context_usage(agent_registry)
    _check_undeclared_template_vars(agent_registry)
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

        # Use the registry's actual load folder, not name-to-path math: namespaced agents
        # (name has '::') can live in a FLAT directory whose name doesn't mirror the namespace
        # (e.g. wiki::fact_answer_judge in agents/wiki_fact_answer_judge/), so replacing '::'→'/'
        # points at a directory that doesn't exist and falsely reports missing prompts.
        loaded_from = config.get("_loaded_from")
        prompt_dir = (Path(loaded_from) if loaded_from
                      else Path("app/assistant/agents") / agent_name.replace("::", "/")) / "prompts"
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


def _check_undeclared_template_vars(registry: AgentRegistry):
    """Detect variables referenced in user.j2 / system.j2 that are NOT
    declared in user_context_items / system_context_items.

    The reverse direction (declared but not used) is already covered by
    _check_context_usage. This one catches the case where a prompt
    references {{ X }} but nothing populates X — exactly the failure
    that made wiki_inclusion_critic render with empty placeholders for
    3,699 calls per session.

    Inverts the SUB_COMPONENT_MAP: if user.j2 references the PARENT
    ('entity_info'), then having any of its children ('entity_summary',
    'entity_metadata') declared in user_context_items counts as
    satisfying the dependency.
    """
    # Same parent/child relationships as _check_context_usage uses.
    SUB_COMPONENT_MAP = {
        'entity_summary': 'entity_info',
        'entity_metadata': 'entity_info',
    }
    # Inverted: parent → set of children that declare it.
    parent_to_children: dict = {}
    for child, parent in SUB_COMPONENT_MAP.items():
        parent_to_children.setdefault(parent, set()).add(child)

    env = Environment()

    for agent_name, config in registry.configs.items():
        if config.get("type") == "control_node":
            continue
        if config.get("class_name") == "Delegator":
            continue

        prompts = config.get("prompts", {}) or {}

        def check_one(prompt_text: str, prompt_name: str, context_key: str) -> None:
            if not prompt_text:
                return
            try:
                ast = env.parse(prompt_text)
            except Exception as e:
                logger.warning(
                    "%s: %s.j2 failed to parse for var extraction: %s",
                    agent_name, prompt_name, e,
                )
                return
            referenced = jinja_meta.find_undeclared_variables(ast)
            # jinja_meta.find_undeclared_variables only excludes assignments at
            # the top-level of the template. {% set X = ... %} inside a loop
            # or conditional still appears in `referenced`. Walk the AST and
            # collect every Name used as the LHS of any Assign / AssignBlock
            # node so locally-set vars don't trigger false-positive warnings.
            locally_set: set = set()
            for node in ast.find_all((jinja_nodes.Assign, jinja_nodes.AssignBlock)):
                target = getattr(node, "target", None)
                if isinstance(target, jinja_nodes.Name):
                    locally_set.add(target.name)
                elif isinstance(target, jinja_nodes.Tuple):
                    # {% set a, b = ... %} — pull each Name from the tuple
                    for item in getattr(target, "items", []) or []:
                        if isinstance(item, jinja_nodes.Name):
                            locally_set.add(item.name)
            # Also collect names introduced by {% with X = ... %} blocks
            for node in ast.find_all(jinja_nodes.With):
                for tnode in getattr(node, "targets", []) or []:
                    if isinstance(tnode, jinja_nodes.Name):
                        locally_set.add(tnode.name)

            declared = set(config.get(context_key) or [])
            # A declared field SATISFIES itself OR its parent (per SUB_COMPONENT_MAP).
            satisfied: set = set()
            for field in declared:
                satisfied.add(field)
                parent = SUB_COMPONENT_MAP.get(field)
                if parent:
                    satisfied.add(parent)

            missing = referenced - satisfied - _FRAMEWORK_BUILTINS - locally_set
            for var in sorted(missing):
                logger.warning(
                    "%s: %s.j2 references {{ %s }} but %s does NOT declare it. "
                    "Template will render with an empty/Undefined value. "
                    "Add '%s' to %s in config.yaml.",
                    agent_name, prompt_name, var, context_key, var, context_key,
                )

        if "user" in prompts:
            check_one(prompts["user"], "user", "user_context_items")
        if "system" in prompts:
            check_one(prompts["system"], "system", "system_context_items")


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

        # Agents bound to manager roles (delegator / critic / summary etc.)
        # are used at runtime even if they don't appear in state_map.
        for val in (config.get("role_bindings") or {}).values():
            if isinstance(val, str):
                all_flow.add(val)

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
