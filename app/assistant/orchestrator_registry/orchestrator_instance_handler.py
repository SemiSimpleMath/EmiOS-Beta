class OrchestratorInstanceHandler:
    """
    Parallel to ManagerInstanceHandler, but for orchestrators.

    Tracks created orchestrator instances and supports "find available" selection.
    """

    def __init__(self):
        # { instance_name: instance_object }
        self.instances = {}

        # { orchestrator_type: [instance_name1, instance_name2, ...] }
        self.type_to_instances = {}

    def register(self, name: str, instance, orchestrator_type: str):
        existing = self.instances.get(name)
        if existing is not None and existing is not instance:
            raise RuntimeError(f"OrchestratorInstanceHandler already has an instance named '{name}'")
        self.instances[name] = instance
        if name not in self.type_to_instances.setdefault(orchestrator_type, []):
            self.type_to_instances[orchestrator_type].append(name)

    def get_instance(self, name: str):
        return self.instances.get(name)

    def get_instances_by_type(self, orchestrator_type: str):
        names = self.type_to_instances.get(orchestrator_type, [])
        return [(name, self.instances[name]) for name in names]

    def find_available_instance(self, orchestrator_type: str):
        for name in self.type_to_instances.get(orchestrator_type, []):
            inst = self.instances.get(name)
            if inst and not inst.is_busy():
                return name, inst
        return None, None

