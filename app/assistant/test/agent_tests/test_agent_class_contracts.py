from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.agent_runtime.factories.agent_components_factory import AgentComponentsFactory
from app.assistant.agent_runtime.types import AgentComponents
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.validation.agent_validator import _check_llm_params_contract


class _NullService:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _stub_components() -> AgentComponents:
    null = _NullService()
    return AgentComponents(
        status_tracker=null,
        llm_client=null,
        chat_request_normalizer=null,
        chat_response_builder=null,
        chat_publisher=null,
        history_formatter=null,
        action_validator=null,
        flow_controller=null,
        progress_emitter=null,
        prompt_builder=null,
        context_injector=null,
        entity_injector=null,
        pod_injector=null,
    )


def test_llm_params_contract_is_valid_for_all_registered_agents():
    registry = AgentRegistry()
    registry.load_agents()
    _check_llm_params_contract(registry)


def test_all_registered_agent_classes_instantiate_once(monkeypatch):
    monkeypatch.setattr(
        AgentComponentsFactory,
        "build_for_agent",
        staticmethod(lambda _agent: _stub_components()),
    )

    registry = AgentRegistry()
    registry.load_agents()
    tool_registry = ToolRegistry()
    failures: list[str] = []

    for agent_name, config in (registry.configs or {}).items():
        if not isinstance(config, dict):
            continue
        cls = config.get("class")
        if cls is None:
            continue

        bb = Blackboard()
        try:
            if config.get("type") == "control_node":
                cls(
                    name=agent_name,
                    blackboard=bb,
                    agent_registry=registry,
                    tool_registry=tool_registry,
                )
            else:
                cls(
                    name=agent_name,
                    blackboard=bb,
                    agent_registry=registry,
                    tool_registry=tool_registry,
                    llm_params=config.get("llm_params", {}),
                    parent=None,
                )
        except Exception as e:
            failures.append(f"{agent_name}: {type(e).__name__}: {e}")

    assert not failures, "Instantiation failures:\n" + "\n".join(failures[:25])
