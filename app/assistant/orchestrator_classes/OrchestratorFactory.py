from __future__ import annotations

import copy
import importlib

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class OrchestratorFactory:
    """
    Parallel to MultiAgentManagerFactory: create-only orchestrator instances from registry configs.
    """

    def __init__(self, registry, tool_registry, agent_registry):
        self.registry = registry
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry
        self._instances: dict[str, object] = {}

    def create_orchestrator(self, orchestrator_type: str, name: str | None = None):
        config = self.registry.get(orchestrator_type) if hasattr(self.registry, "get") else None
        if not config:
            raise ValueError(f"No config found for orchestrator type '{orchestrator_type}'")

        class_name = config.get("class_name")
        if not class_name:
            raise ValueError(f"Missing 'class_name' in config for '{orchestrator_type}'")

        orch_class = self._import_class(class_name)

        name = name or orchestrator_type
        # Guard against duplicate names (can silently overwrite otherwise).
        if name in self._instances and self._instances.get(name) is not None:
            raise RuntimeError(f"OrchestratorFactory already created an orchestrator named '{name}'")

        # Like manager factory: isolate registries per instance.
        if hasattr(self.agent_registry, "fork") and callable(getattr(self.agent_registry, "fork")):
            agent_registry_copy = self.agent_registry.fork()
        else:
            # Best-effort shallow clone: copy config dict and clear instances.
            agent_registry_copy = copy.copy(self.agent_registry)
            try:
                cfgs = getattr(agent_registry_copy, "configs", None)
                if isinstance(cfgs, dict):
                    new_cfgs = {}
                    for k, v in cfgs.items():
                        if isinstance(v, dict):
                            v2 = dict(v)
                            v2.pop("instance", None)
                            new_cfgs[k] = v2
                        else:
                            new_cfgs[k] = v
                    agent_registry_copy.configs = new_cfgs
            except Exception:
                pass

        instance = orch_class(name, config, self.tool_registry, agent_registry_copy)

        try:
            if hasattr(self.registry, "register_instance") and callable(getattr(self.registry, "register_instance")):
                self.registry.register_instance(name, instance)
        except Exception:
            pass
        # Best-effort: register in instance handler if present.
        try:
            h = getattr(DI, "orchestrator_instance_handler", None)
            if h is not None:
                reg = getattr(h, "register", None)
                if callable(reg):
                    reg(name=name, instance=instance, orchestrator_type=orchestrator_type)
        except Exception:
            pass
        self._instances[name] = instance
        return instance

    def _import_class(self, class_name: str):
        try:
            module_path = f"app.assistant.orchestrator_classes.{class_name}"
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except Exception as e:
            raise ImportError(f"❌ Could not import orchestrator class '{class_name}': {e}")

