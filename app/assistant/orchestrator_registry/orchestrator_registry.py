from __future__ import annotations

from pathlib import Path

from app.assistant.utils.config_loader import load_config


class OrchestratorRegistry:
    """
    Parallel to ManagerRegistry, but for orchestrators.

    Loads orchestrator-type configs from:
      app/assistant/orchestrators/<orchestrator_type>/config.yaml
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._configs: dict[str, dict] = {}
        self._instances: dict[str, object] = {}

    def preload_all(self) -> None:
        if not self.base_dir.exists():
            return
        for subdir in self.base_dir.iterdir():
            if subdir.is_dir() and (subdir / "config.yaml").exists():
                name = subdir.name
                self._configs[name] = load_config(subdir / "config.yaml")

    def get(self, orchestrator_type: str) -> dict | None:
        return self._configs.get(orchestrator_type)

    def list_available(self) -> list[str]:
        return list(self._configs.keys())

    def register_instance(self, name: str, instance: object) -> None:
        existing = self._instances.get(name)
        if existing is not None and existing is not instance:
            raise RuntimeError(f"OrchestratorRegistry already has an instance named '{name}'")
        self._instances[name] = instance

    def get_instance(self, name: str) -> object | None:
        return self._instances.get(name)

    def list_instances(self) -> list[str]:
        return list(self._instances.keys())

