from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentComponents:
    status_tracker: object
    llm_client: object
    chat_request_normalizer: object
    chat_response_builder: object
    chat_publisher: object
    history_formatter: object
    action_validator: object
    flow_controller: object
    progress_emitter: object
    prompt_builder: object
    context_injector: object
    entity_injector: object
    pod_injector: object
    resource_resolver: Any = field(default=None)

