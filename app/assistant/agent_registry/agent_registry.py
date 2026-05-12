# NOTE TO AI CODING AGENTS:
# This file is fundamental to EmiAi’s agent loading and should NOT be modified
# without explicit user permission.
#
# app/assistant/agent_registry/agent_registry.py
import importlib.util
import hashlib
import sys
from typing import List

import yaml
from pathlib import Path
from pydantic import BaseModel

# Set up logging
from app.assistant.control_nodes.control_node import ControlNode

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root
logger = get_logger(__name__)


APP_ROOT = get_app_root()


class AgentRegistry:
    def __init__(self, agents_dir=None, control_nodes_dir=None):
        """Ensure proper paths to avoid duplicate 'app' in directory paths."""
        self.agents_dir = Path(agents_dir or (APP_ROOT / "assistant/agents")).resolve()
        self.control_nodes_dir = Path(control_nodes_dir or (APP_ROOT / "assistant/control_nodes")).resolve()

        self.configs = {}

        self.registry_loaded = False

    def load_agents(self):
        # Load agents into self.configs.
        self._load_all_agent_configs()
        # Load control nodes into self.configs (tagged with type='control_node'
        # so callers can distinguish; see ToolArguments / agent_flow_manager /
        # agent_loader / tool_caller — they look up by name on self.configs
        # and check the 'type' discriminator).
        self._load_all_control_nodes()

        self.registry_loaded = True

    def fork(self):
        """
        Create a lightweight copy of the registry suitable for parallel managers/orchestrators.

        - Shares immutable config data and loaded classes/prompts
        - Clears per-agent `instance` fields so each runtime can instantiate independently
        - Avoids deepcopy (locks/clients/lambdas can break deepcopy and it's expensive)
        """
        child = AgentRegistry(agents_dir=self.agents_dir, control_nodes_dir=self.control_nodes_dir)
        # Preserve loaded state. Control nodes live in self.configs since the
        # 2026-05-10 cleanup; no separate copy needed.
        child.registry_loaded = bool(getattr(self, "registry_loaded", False))

        # Shallow-copy configs, but remove any instantiated agent objects.
        new_configs = {}
        for name, cfg in (getattr(self, "configs", {}) or {}).items():
            if not isinstance(cfg, dict):
                new_configs[name] = cfg
                continue
            cfg2 = dict(cfg)
            cfg2.pop("instance", None)
            new_configs[name] = cfg2
        child.configs = new_configs
        return child

    def _load_all_agent_configs(self):
        """Load all agent configurations, prompts, structured outputs, and dynamically load agent classes."""

        if self.registry_loaded:
            import traceback
            stack = "".join(traceback.format_stack()[:-1])
            logger.warning("REGISTRY ALREADY LOADED - Attempted to reload!\n%s", stack)
            return

        if not self.agents_dir.exists():
            logger.error(f"Agents directory '{self.agents_dir}' does not exist.")
            return

        for agent_folder in self.agents_dir.rglob("*"):
            if not agent_folder.is_dir():
                continue
            if not (agent_folder / "config.yaml").exists():
                continue

            if (agent_folder / ".ignore").exists():
                logger.info(f"Skipping agent {agent_folder.name} (marked as .ignore)")
                continue

            config_data = self._load_config(agent_folder / "config.yaml")
            raw_name = config_data.get('name', None)
            if not raw_name:
                logger.warning(f"Agent name not found in {agent_folder.name}")
                continue

            rel_path = agent_folder.relative_to(self.agents_dir)

            # === Canonical name = namespace::name ===
            if "::" in raw_name:
                canonical_name = raw_name  # already fully namespaced
            else:
                if rel_path.parent == Path("."):
                    canonical_name = raw_name
                else:
                    # Handle multi-level directories with proper :: separators
                    namespace_parts = []
                    current_path = rel_path.parent
                    while current_path != Path("."):
                        namespace_parts.insert(0, current_path.name)
                        current_path = current_path.parent
                    
                    namespace = "::".join(namespace_parts)
                    canonical_name = f"{namespace}::{raw_name}"

            logger.info(f"📥 Loading configuration for agent: {canonical_name}")
            prompts = self._load_prompts(agent_folder / "prompts", canonical_name)
            # Structured output precedence:
            # 1) agent_form.py (Pydantic) if present (preferred / strongest)
            # 2) config.yaml structured_output (JSON schema dict) as a fallback
            structured_output = self._load_agent_form(agent_folder / "agent_form.py")
            if structured_output is None:
                structured_output = config_data.get("structured_output")
            else:
                if config_data.get("structured_output") is not None:
                    logger.info(
                        f"Agent {canonical_name} defines both agent_form.py and config.yaml structured_output; "
                        f"preferring agent_form.py."
                    )
            input_schema = self._load_agent_args(agent_folder / "input_schema.py")
            if canonical_name in self.configs:
                import traceback
                logger.warning(f"Duplicate agent name {canonical_name} in folder {agent_folder.name}")
                logger.warning(f"  First loaded from: {self.configs[canonical_name].get('_loaded_from', 'unknown')}")
                logger.warning(f"  Now loading from: {agent_folder}")
                stack = "".join(traceback.format_stack()[:-1][-5:])
                logger.debug("Call stack for duplicate load:\n%s", stack)

            self.configs[canonical_name] = {
                **config_data,
                "prompts": prompts,
                "structured_output": structured_output,
                "type": "agent",
                "input_schema": input_schema,
                "_loaded_from": str(agent_folder)
            }

            agent_class = self._load_agent_class(canonical_name)
            self.configs[canonical_name]['class'] = agent_class
            logger.info(f"✅ Loaded agent: {canonical_name}, Class: {agent_class}")


        return


    def _load_prompts(self, prompts_dir, agent_name):
        """Load the system and user prompts for an agent."""
        prompts = {}
        if not prompts_dir.exists():
            logger.debug(f"No prompts directory for agent: {agent_name}")
            return prompts

        system_prompt = prompts_dir / "system.j2"
        user_prompt = prompts_dir / "user.j2"
        description = prompts_dir/ "description.j2"

        if system_prompt.exists():
            try:
                with open(system_prompt, "r", encoding="utf-8") as f:
                    prompts["system"] = f.read()
                logger.info(f"Loaded system prompt for {agent_name}")
            except Exception as e:
                logger.error(f"Error reading system.j2 for {agent_name}: {e}", exc_info=True)
                raise RuntimeError(f"Could not read system prompt for '{agent_name}': {e}") from e
        else:
            logger.error(f"Error: No system prompt for {agent_name}")
            raise FileNotFoundError(f"Missing required system prompt for {agent_name}: {system_prompt}")

        if user_prompt.exists():
            try:
                with open(user_prompt, "r", encoding="utf-8") as f:
                    prompts["user"] = f.read()
                logger.info(f"Loaded user prompt for {agent_name}")
            except Exception as e:
                logger.error(f"Error reading user.j2 for {agent_name}: {e}", exc_info=True)
                raise RuntimeError(f"Could not read user prompt for '{agent_name}': {e}") from e

        else:
            logger.error(f"Error: No user prompt for {agent_name}")
            raise FileNotFoundError(f"Missing required user prompt for {agent_name}: {user_prompt}")

        if description.exists():
            try:
                with open(description, "r", encoding="utf-8") as f:
                    prompts["description"] = f.read()
                logger.info(f"Loaded description for {agent_name}")
            except Exception as e:
                logger.error(f"Error reading description.j2 for {agent_name}: {e}", exc_info=True)
                raise RuntimeError(f"Could not read description for '{agent_name}': {e}") from e

        return prompts

    def _load_agent_args(self, agent_args_path):
        """Dynamically import and load the Pydantic input model from agent_args.py."""
        if not agent_args_path.exists():
            logger.info(f"No agent_args.py found for {agent_args_path.parent.name}")
            return None

        try:
            # IMPORTANT:
            # Use a unique module name per path to avoid collisions between different
            # agents' `agent_args.py` and to keep Pydantic's annotation resolution stable.
            raw = str(agent_args_path.resolve())
            h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            module_name = f"app.assistant.agents._dynamic.agent_args_{agent_args_path.parent.name}_{h}"
            spec = importlib.util.spec_from_file_location(module_name, agent_args_path)
            module = importlib.util.module_from_spec(spec)
            # Ensure sys.modules contains the module under its unique name so
            # downstream libraries (Pydantic) can resolve annotations correctly.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Look for a Pydantic model in the module
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, BaseModel) and attr is not BaseModel:
                    logger.info(f"Loaded input schema class {attr_name} from {agent_args_path}")
                    return attr

            logger.warning(f"No valid Pydantic input model found in {agent_args_path}")
            return None

        except Exception as e:
            logger.error("Error loading input schema from %s: %s", agent_args_path, e, exc_info=True)
            raise RuntimeError(
                f"Failed to load agent_args.py for agent '{agent_args_path.parent.name}': {e}"
            ) from e

    def _load_agent_form(self, agent_form_path: Path):
        """Dynamically import and load the AgentForm class from agent_form.py."""
        if not agent_form_path.exists():
            logger.debug(f"No agent_form.py for {agent_form_path.parent.name}")
            return None

        try:
            # IMPORTANT:
            # Use a unique module name per path to avoid collisions between different
            # agents' `agent_form.py`. Collisions break Pydantic v2's ability to resolve
            # forward refs / postponed annotations (e.g., "List[str]") reliably.
            raw = str(agent_form_path.resolve())
            h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            module_name = f"app.assistant.agents._dynamic.agent_form_{agent_form_path.parent.name}_{h}"
            spec = importlib.util.spec_from_file_location(module_name, agent_form_path)
            module = importlib.util.module_from_spec(spec)
            # Ensure sys.modules contains the module under its unique name so
            # Pydantic can resolve annotations by module namespace.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            preferred = "AgentForm"
            fallback = None

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseModel)
                        and attr is not BaseModel
                ):
                    if attr_name == preferred:
                        logger.info(f"Loaded preferred structured output class {attr_name}")
                        return attr
                    if fallback is None:
                        fallback = attr

            if fallback:
                logger.warning(f"AgentForm not found, falling back to {fallback.__name__}")
                return fallback

            logger.warning(f"No valid Pydantic model found in {agent_form_path}")
            return None

        except Exception as e:
            logger.error(f"Error loading structured output from {agent_form_path}: {e}", exc_info=True)
            raise RuntimeError(
                f"Failed to load agent_form.py for agent '{agent_form_path.parent.name}': {e}"
            ) from e



    def register_agent_class(self, agent_name, agent_class_reference):
        """Assigns the agent class reference to its config."""
        if agent_name in self.configs:
            self.configs[agent_name]["class"] = agent_class_reference
            logger.info(f"✅ Registered class {agent_class_reference} for agent {agent_name}")
        else:
            logger.warning(f"⚠️ Tried to register {agent_name}, but it's not in the registry.")

    def _load_agent_class(self, agent_name):
        """Dynamically load an agent class from `agent_classes/` based on `class_name` from config.yaml."""
        agent_config = self.configs.get(agent_name)
        if agent_config is None:
            raise RuntimeError(f"❌ Missing config for agent '{agent_name}'. "
                               f"Use full name (e.g., 'shared::tool_selector').")

        expected_class_name = agent_config.get("class_name")
        if not expected_class_name:
            raise ValueError(f"❌ Agent {agent_name} does not specify a `class_name` in its config.")

        agent_class_file = self.agents_dir.parent / "agent_classes" / f"{expected_class_name}.py"

        if not agent_class_file.exists():
            raise FileNotFoundError(f"❌ Expected class file `{agent_class_file}` for {agent_name} not found.")

        try:
            module_name = f"app.assistant.agent_classes.{expected_class_name}"
            spec = importlib.util.spec_from_file_location(module_name, agent_class_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # ✅ Ensure class exists and matches the expected name
            agent_class = getattr(module, expected_class_name, None)
            if not agent_class:
                raise ImportError(f"❌ Expected class `{expected_class_name}` not found inside `{agent_class_file}`. "
                                  f"Check that the class name matches the config.")

            logger.info(f"✅ Loaded class {expected_class_name} for agent {agent_name}")
            return agent_class

        except Exception as e:
            logger.error(f"❌ Error loading agent class {expected_class_name} from {agent_class_file}: {e}")
            raise

    def list_agents(self) -> List[str]:
        return list(self.configs.keys())

    def list_instantiated_agents(self) -> List[str]:
        """
        Return canonical names of agents/control nodes that currently have an instance.
        """
        names: list[str] = []
        for name, cfg in (self.configs or {}).items():
            if isinstance(cfg, dict) and cfg.get("instance") is not None:
                names.append(name)
        return names

    def is_callable_agent(self, name: str) -> bool:
        """
        Runtime callability contract:
        - If any instances exist, only instantiated names are callable.
        - Otherwise (bootstrap/config-only modes), configured names are callable.
        """
        if not isinstance(name, str) or not name.strip():
            return False
        requested = name.strip()
        instantiated = set(self.list_instantiated_agents())
        if len(instantiated) > 0:
            return requested in instantiated
        return requested in set(self.list_agents())


    def _load_config(self, config_file):
        """Load the agent's configuration file, including extra configs."""
        if not config_file.exists():
            logger.warning(f"Missing config.yaml for agent: {config_file.parent.name}")
            return {}

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            # ✅ Load extra configs if defined
            extra_configs = config.pop("extra_configs", [])
            for extra_config_path in extra_configs:
                extra_path = config_file.parent / extra_config_path
                if extra_path.exists():
                    with open(extra_path, "r", encoding="utf-8") as extra_f:
                        extra_data = yaml.safe_load(extra_f) or {}
                    config_key = Path(extra_config_path).stem
                    config[config_key] = extra_data  # Store under dynamically determined key
                    logger.info(f"🔹 Merged extra config '{extra_config_path}' under key '{config_key}'")

            return config

        except Exception as e:
            logger.error("Error loading config file %s: %s", config_file, e, exc_info=True)
            raise RuntimeError(
                f"Failed to load config for agent '{config_file.parent.name}': {e}"
            ) from e

    def _load_all_control_nodes(self):
        """Scan the `control_nodes/` directory for Python files and dynamically load them."""

        if not self.control_nodes_dir.exists():
            logger.error(f"❌ Control nodes directory '{self.control_nodes_dir}' does not exist.")
            return {}

        for control_file in self.control_nodes_dir.glob("*.py"):
            if control_file.stem == "control_node" or control_file.name == "__init__.py":
                continue  # Skip base class and init file
            # Underscore-prefixed files are utility / helper modules co-located
            # with control nodes (e.g. _switchboard_arguments_util.py,
            # _tool_caller_util.py). They expose helpers consumed by control
            # nodes; they don't define ControlNode subclasses themselves.
            # Trying to load them as control nodes yields a noisy startup
            # warning and a useless config entry.
            if control_file.stem.startswith("_"):
                continue

            control_name = control_file.stem  # Get filename without .py
            logger.info(f"📥 Loading control node: {control_name}")

            try:
                control_class = self._load_control_node_class(control_file)

                if control_class:
                    # Store control node info in the same format as agents.
                    self.configs[control_name] = {
                        "name": control_name,
                        "class_name": control_class.__name__,
                        "type": "control_node",
                        "class": control_class,
                        "instance": None,  # Placeholder until instantiated
                    }
                    logger.info(f"✅ Loaded control node: {control_name}")
                else:
                    logger.warning(f"⚠️ Failed to load control node class for: {control_name}")
            except Exception as e:
                logger.error(f"❌ Error loading control node {control_name}: {e}")
                continue



    def _load_control_node_class(self, control_node_path):
        """Dynamically load a control node class from its Python file, ensuring we do not load the base ControlNode."""
        if not control_node_path.exists():
            logger.warning(f"⚠️ Missing control node file: {control_node_path}")
            return None

        try:
            module_name = control_node_path.stem  # Use filename as module name
            spec = importlib.util.spec_from_file_location(module_name, control_node_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find subclasses defined in THIS module only.
            # This prevents accidentally selecting imported base/helper subclasses
            # (e.g. ChatTaskRouterNode imported into master_room_chat_task_router_node.py).
            local_subclasses = []
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, ControlNode)
                    and attr is not ControlNode
                    and getattr(attr, "__module__", "") == module.__name__
                ):
                    local_subclasses.append(attr)

            if not local_subclasses:
                logger.warning(f"⚠️ No valid ControlNode subclass found in {control_node_path}")
                return None

            expected_class_name = "".join(part.capitalize() for part in control_node_path.stem.split("_"))
            for cls in local_subclasses:
                if cls.__name__ == expected_class_name:
                    return cls

            if len(local_subclasses) == 1:
                return local_subclasses[0]

            class_names = ", ".join(sorted(cls.__name__ for cls in local_subclasses))
            raise RuntimeError(
                f"Ambiguous control node classes in {control_node_path}: [{class_names}]. "
                f"Expected class '{expected_class_name}'."
            )

        except Exception as e:
            logger.error("Error loading control node from %s: %s", control_node_path, e, exc_info=True)
            raise RuntimeError(
                f"Failed to load control node '{control_node_path.stem}': {e}"
            ) from e

    def get_agent_config(self, name):
        """Retrieve a specific agent's config."""
        return self.configs.get(name, None)

    def get_agent_class(self, name):
        """Retrieve the registered class reference for an agent."""
        return self.configs.get(name, {}).get("class", None)

    def get_all_agents(self):
        """Return a dictionary of all available agent configurations."""
        return self.configs

    def register_agent_instance(self, agent_name, agent_instance):
        """Store the instantiated agent inside the registry. Works for both
        regular agents and control nodes — both live in self.configs."""
        if agent_name in self.configs:
            self.configs[agent_name]["instance"] = agent_instance
            logger.info(f"✅ Registered instance for {self.configs[agent_name].get('type','agent')} {agent_name}")
        else:
            # Manager-local alias (e.g. 'return_control' -> ReturnControlNode).
            # Store in configs so get_agent_instance can retrieve it at runtime.
            self.configs[agent_name] = {"instance": agent_instance}
            logger.info(f"✅ Registered instance for manager-local alias {agent_name}")

    def get_agent_instance(self, agent_name):
        """Retrieve an initialized agent instance (works for control nodes too —
        they live in self.configs since the 2026-05-10 cleanup)."""
        return self.configs.get(agent_name, {}).get("instance", None)

    def get_agent_input_form(self, agent_name):
        """
        Returns the Pydantic input schema class for an agent, if defined via agent_args.py.
        """
        return self.configs.get(agent_name, {}).get("input_schema", None)



def main():
    registry = AgentRegistry()

    print("\n=== All Agent Configs ===")
    for agent_name, config in registry.get_all_agents().items():
        print(f"\nAgent: {agent_name}")
        print("Config:", config)

    print("\n=== Testing Class Lookup ===")
    print("emi_agent class:", registry.get_agent_class("emi_agent"))
    print("ToolCaller control node:", registry.get_agent_class("tool_caller"))


if __name__ == "__main__":
    main()
