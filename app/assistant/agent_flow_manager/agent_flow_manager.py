import copy
import json
import os
from uuid import uuid4
from pathlib import Path
from collections import OrderedDict

import yaml

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root
logger = get_logger(__name__)

APP_ROOT = get_app_root()
AGENT_DISK_PATH = APP_ROOT / "assistant" / "agents"


def agent_name_to_path(agent_name: str) -> str:
    return os.path.join(*agent_name.split("::"))


def sort_config_fields(config: dict):
    # Optional: reorder top-level keys
    desired_order = [
        "name", "class_name", "color", "llm_params",
        "allowed_tools", "except_tools", "allowed_nodes",
        "user_context_items", "rag_fields", "system_context_items",
        "prompts", "type"  # type usually last
    ]
    sorted_config = OrderedDict()
    for key in desired_order:
        if key in config:
            sorted_config[key] = config[key]
    for k, v in config.items():
        if k not in sorted_config:
            sorted_config[k] = v  # preserve unknown keys
    return sorted_config

def ordered_to_dict(d):
    if isinstance(d, OrderedDict):
        return {k: ordered_to_dict(v) for k, v in d.items()}
    elif isinstance(d, dict):
        return {k: ordered_to_dict(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [ordered_to_dict(i) for i in d]
    else:
        return d

def is_json_serializable(value):
    try:
        json.dumps(value)
        return True
    except (TypeError, OverflowError):
        return False

def _hydrate_agent_config(name: str, config: dict) -> dict:
    # Copy to avoid mutating caller; only keep serializable values.
    result = {k: v for k, v in config.items() if is_json_serializable(v)}

    result.setdefault("prompts", {"system": "", "user": ""})

    agent_type = config.get('type', 'unknown')

    # Handle control nodes properly
    if agent_type == "control_node":
        # Ensure control nodes have required fields
        result.setdefault("name", name)
        result.setdefault("class_name", config.get("class_name", "UnknownClass"))
        result.setdefault("type", "control_node")
        return result

    # Handle agents (existing logic)
    if agent_type != "agent":
        return result

    if "class_name" not in config:
        raise ValueError(f"Agent '{name}' is missing required field: 'class_name'")
    if "name" not in config:
        raise ValueError(f"Agent '{name}' is missing required field: 'name'")

    schema_config = {}
    agent_subpath = Path(*name.split("::"))  # safe, platform-independent
    agent_dir = AGENT_DISK_PATH / agent_subpath


    json_path = agent_dir / "agent_form.json"
    if json_path.exists():
        try:
            schema_config["json_schema"] = json.loads(json_path.read_text())
        except Exception as e:
            logger.warning(f"Failed to load JSON schema for '{name}': {e}")

    py_path = agent_dir / "agent_form.py"
    if py_path.exists():
        try:
            schema_config["pydantic_code"] = py_path.read_text()
        except Exception as e:
            logger.warning(f"Failed to load Pydantic code for '{name}': {e}")
    else:
        logger.debug(f"agent_form.py path does not exist: {py_path}")

    if schema_config:
        result["schema_config"] = schema_config

    return result

class AgentFlowManager:
    def __init__(self):
        pass

    def get_manager_config(self, name):
        manager_config = DI.manager_registry.get_manager_config(name)
        manager_config_copy = copy.deepcopy(manager_config)
        flow_config = manager_config_copy.pop('flow_config')
        node_config = self.build_graph_from_manager(manager_config, flow_config)
        return {'manager_config': manager_config_copy, 'flow': node_config}

    def get_all_manager_configs(self):
        all_configs = DI.manager_registry.all_configs()
        logger.info(f"🔍 Manager registry has {len(all_configs)} configs: {list(all_configs.keys())}")
        result = {}

        for manager_name, manager_config in all_configs.items():
            logger.info(f"📋 Processing manager: {manager_name}")
            if manager_config is None:
                logger.error(f"❌ Manager config is None for: {manager_name}")
                continue
            
            manager_config_copy = copy.deepcopy(manager_config)
            flow_config = manager_config_copy.pop('flow_config', {})
            
            # Handle new separated format
            if "agents" in manager_config_copy and "control_nodes" in manager_config_copy:
                agents = manager_config_copy.get('agents', [])
                control_nodes = manager_config_copy.get('control_nodes', [])
                
                # Handle case where control_nodes might be None
                if control_nodes is None:
                    control_nodes = []
                
                all_components = agents + control_nodes
            else:
                # Legacy mixed format
                agents = manager_config_copy.get('agents', [])
                all_components = agents

            agent_configs = {}
            agent_id_lookup = {}

            # For each component in the manager config list, generate a UUID and use that as the key.
            for component in all_components:
                new_uuid = str(uuid4())
                agent_id_lookup[component["name"]] = new_uuid

                config = self.get_agent_config(component["name"])
                config["name"] = component["name"]
                agent_configs[new_uuid] = config

            # Build graph using the lookup from agent display name to UUID
            const_flow = self.build_graph_from_manager(agent_id_lookup, flow_config, agent_configs, manager_config)

            result[manager_name] = {
                "manager_config": manager_config_copy,
                "agents": agent_configs,  # keys are UUIDs now
                "flow": const_flow,
                "saved": True,
            }

        return result

    def build_graph_from_manager(self, agent_id_lookup: dict, flow_config: dict, agents_dict: dict, manager_config):

        role_bindings = (manager_config or {}).get("role_bindings", {})

        nodes = []
        for name, uuid in agent_id_lookup.items():
            agent_config = agents_dict.get(uuid, {})
            node_type = agent_config.get("type", "agent")
            nodes.append({
                "id": uuid,
                "data": { "label": name, "type": node_type },
                "type": node_type,
                "position": { "x": 0, "y": 0 }
            })

        edges = []
        # Flow-based state map edges (using names from state_map)
        for source, target in flow_config.get('state_map', {}).items():
            source_id = agent_id_lookup.get(source)
            target_id = agent_id_lookup.get(target)
            if source_id and target_id:
                edges.append({
                    "id": f"{source_id}-{target_id}",
                    "source": source_id,
                    "target": target_id,
                    "markerEnd": { "type": "arrowclosed" },
                    "edge_type": "flow"
                })

        # Permission-based allowed_edges edges from allowed_nodes in config
        for source_name, source_id in agent_id_lookup.items():
            agent_config = agents_dict.get(source_id, {})
            for target_name in agent_config.get("allowed_nodes", []):
                target_id = agent_id_lookup.get(target_name)
                if target_id:
                    edges.append({
                        "id": f"{source_id}-allowed-{target_id}",
                        "source": source_id,
                        "target": target_id,
                        "markerEnd": { "type": "arrowclosed" },
                        "edge_type": "allowed"
                    })

            tool_selector_name = None
            if isinstance(agent_id_lookup, dict):
                tool_selector_name = role_bindings.get("tool_selector")

            tool_selector_id = agent_id_lookup.get(tool_selector_name) if tool_selector_name else None

        for agent_name, agent_id in agent_id_lookup.items():
            if agent_name == tool_selector_name:
                continue  # don't create edge to itself

            agent_config = agents_dict.get(agent_id, {})
            allowed_tools = agent_config.get("allowed_tools", [])

            if allowed_tools and tool_selector_id:
                edges.append({
                    "id": f"{agent_id}-tool_edge-{tool_selector_id}",
                    "source": agent_id,
                    "target": tool_selector_id,
                    "markerEnd": { "type": "arrowclosed" },
                    "edge_type": "tool_edge"
                })
            elif allowed_tools and not tool_selector_id:
                logger.warning(f"Tool selector not found for agent {agent_name} — no role binding?")



        return {
            "nodes": nodes,
            "edges": edges,
            "layouted": False  # flag so frontend can layout initially
        }

    def get_agent_config(self, name):
        raw = DI.agent_registry.get_agent_config(name)
        if raw is None:
            logger.error(f"❌ No agent config found for: {name}")
            return {"name": name, "type": "unknown"}
        
        raw.pop("class", None)
        raw.pop("structured_output", None)
        return _hydrate_agent_config(name, raw)

    def save_agent_config(self, config, agents_base_path):
        logger.debug("save_agent_config: %s", config.get("name", "[unknown]"))
        agent_name = config['name']
        config.setdefault("type", "agent")

        # Clean the config before saving
        config_copy = config.copy()
        
        # Remove UUID-related fields
        config_copy.pop("agent_id", None)
        
        # Remove schema_config (it's saved separately)
        schema_config = config_copy.pop("schema_config", {})
        
        config_copy = sort_config_fields(config_copy)
        prompts = config_copy.pop("prompts", {})
        system_prompt = prompts.get("system", "")
        user_prompt = prompts.get("user", "")

        # Save inside manager's agent folder
        save_dir = os.path.join(agents_base_path, agent_name_to_path(agent_name))
        os.makedirs(save_dir, exist_ok=True)
        prompt_dir = os.path.join(save_dir, "prompts")
        os.makedirs(prompt_dir, exist_ok=True)

        with open(os.path.join(save_dir, "config.yaml"), "w") as f:
            cleaned_config = ordered_to_dict(config_copy)
            yaml.dump(cleaned_config, f, sort_keys=False, default_flow_style=False)

        # Save prompts
        with open(os.path.join(prompt_dir, "system.j2"), "w") as f:
            f.write(system_prompt.strip() + "\n")
        with open(os.path.join(prompt_dir, "user.j2"), "w") as f:
            f.write(user_prompt.strip() + "\n")

        # Save schema files separately
        if "json_schema" in schema_config:
            json_path = os.path.join(save_dir, "agent_form.json")
            with open(json_path, "w") as f:
                json.dump(schema_config["json_schema"], f, indent=2)
        if "pydantic_code" in schema_config:
            py_path = os.path.join(save_dir, "agent_form.py")
            with open(py_path, "w") as f:
                f.write(schema_config["pydantic_code"].strip() + "\n")

    def get_all_agent_configs(self):
        raw_configs = DI.agent_registry.get_all_agents()
        return {
            name: _hydrate_agent_config(name, cfg)
            for name, cfg in raw_configs.items()
        }

    def save_manager(self, manager):
        logger.debug("save_manager called with %d agents", len(manager.get('agents', {})))
        manager_config = manager['manager_config']
        flow = manager['flow']
        agent_configs = manager['agents']  # UUID → config

        manager_name = manager_config['name']
        base_path = os.path.join(os.getenv("EMI_FLOW_DEBUG_PATH") or (str(get_app_root()) + "/debug/manager_configs"), manager_name) + "/"
        agents_path = os.path.join(base_path, "agents")
        os.makedirs(agents_path, exist_ok=True)

        # Separate agents and control nodes
        agents = []
        control_nodes = []
        
        for agent_id, agent_config in agent_configs.items():
            agent_type = agent_config.get("type", "agent")
            
            component_entry = {
                "class": agent_config.get("class_name", "UnknownClass"),
                "name": agent_config.get("name", agent_id)
            }
            
            if agent_type == "control_node":
                control_nodes.append(component_entry)
            else:
                agents.append(component_entry)
                # Only save actual agents to filesystem
                self.save_agent_config(agent_config, agents_path)

        # Build the manager config with separated lists
        manager_config["agents"] = agents
        if control_nodes:
            manager_config["control_nodes"] = control_nodes

        # Build flow config using agent name mapping
        manager_config["flow_config"] = manager_config.get("flow_config", {})

        # Save full manager config
        with open(os.path.join(base_path, "config.yaml"), "w") as f:
            yaml.safe_dump(manager_config, f)
