from __future__ import annotations

from typing import Any

from app.assistant.agent_runtime.types import AgentComponents
from app.assistant.agent_runtime.services import (
    ActionValidator,
    ContextInjector,
    ChatRequestNormalizer,
    ChatResponseBuilder,
    ChatPublisher,
    EntityInjector,
    PodInjector,
    FlowController,
    HistoryFormatter,
    LLMClient,
    PromptBuilder,
    ProgressEmitter,
    ResourceResolver,
    StatusTracker,
)


class AgentComponentsFactory:
    """
    Composition root for Agent runtime collaborators.

    Dependencies are resolved once at construction time and pushed into each
    service constructor, so the services themselves have no dependency on the
    service locator.
    """

    def __init__(self, *, event_hub: Any, resource_manager: Any, blackboard: Any) -> None:
        self._event_hub = event_hub
        self._resource_manager = resource_manager
        self._blackboard = blackboard

    def build_for_agent(self, agent) -> AgentComponents:
        return AgentComponents(
            status_tracker=StatusTracker(event_hub=self._event_hub),
            llm_client=LLMClient(),
            chat_request_normalizer=ChatRequestNormalizer(),
            chat_response_builder=ChatResponseBuilder(),
            chat_publisher=ChatPublisher(event_hub=self._event_hub),
            history_formatter=HistoryFormatter(),
            action_validator=ActionValidator(),
            flow_controller=FlowController(),
            progress_emitter=ProgressEmitter(event_hub=self._event_hub),
            prompt_builder=PromptBuilder(),
            context_injector=ContextInjector(blackboard=self._blackboard),
            entity_injector=EntityInjector(),
            pod_injector=PodInjector(),
            resource_resolver=ResourceResolver(resource_manager=self._resource_manager),
        )

    @staticmethod
    def build_for_agent_global(agent) -> AgentComponents:
        """
        Shim for call sites that don't yet have a factory instance injected.
        Resolves the singleton factory from DI. Will be removed once all
        callers are migrated to inject the factory instance explicitly.
        """
        from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
        factory: "AgentComponentsFactory" = getattr(DI, "agent_components_factory", None)
        if factory is None:
            raise RuntimeError(
                "DI.agent_components_factory is not initialised. "
                "Ensure bootstrap wires AgentComponentsFactory before creating agents."
            )
        return factory.build_for_agent(agent)
