# llm_factory.py
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import List, Dict

import yaml
from pydantic import BaseModel

from app.services.llm_client import LLMInterface
from app.configs.llm_classes_dict import get_llm_class, get_default_engine_for_provider, _key_is_present

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Model tier resolver — loaded once from configs/model_tiers.yaml
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_tiers() -> dict:
    """configs/model_tiers.yaml `tiers`, from the code tree. The only place a model is chosen."""
    config_path = Path(__file__).resolve().parents[2] / "configs" / "model_tiers.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {})["tiers"]


def resolve_tier(provider: str, model_tier: str) -> tuple[str, str]:
    """The (model, reasoning effort) a tier runs on *provider*. Raises on an unknown tier or provider."""
    tier = load_tiers().get(model_tier)
    if tier is None:
        raise ValueError(f"unknown model_tier {model_tier!r}; defined: {sorted(load_tiers())}")
    model = tier.get(provider)
    if not model:
        raise ValueError(f"model_tier {model_tier!r} names no model for provider {provider!r}")
    return model, tier["effort"]


class LLMFactory:
    _llm_interfaces = {}  # Dict of provider_name -> LLMInterface singleton
    _lock = threading.Lock()

    @staticmethod
    def resolve_effective_params(**kwargs) -> dict:
        """Resolve the effective provider after rerouting, and the tier's model and effort on it.

        Returns a copy of kwargs with llm_provider, engine and reasoning_effort set to what the
        actual API call will use. Requires model_tier.
        """
        kwargs = dict(kwargs)
        requested_provider = kwargs.get('llm_provider', os.environ.get("DEFAULT_LLM_PROVIDER", "openai")).lower()

        # Provider rerouting
        default_provider = os.environ.get("DEFAULT_LLM_PROVIDER", "").strip().lower()
        effective_provider = requested_provider
        if (
            default_provider
            and default_provider != requested_provider
            and not _key_is_present(requested_provider)
            and _key_is_present(default_provider)
        ):
            effective_provider = default_provider
        kwargs['llm_provider'] = effective_provider
        kwargs['engine'], kwargs['reasoning_effort'] = resolve_tier(effective_provider, kwargs['model_tier'])
        return kwargs

    @staticmethod
    def get_llm_interface(**kwargs):
        kwargs = LLMFactory.resolve_effective_params(**kwargs)
        effective_provider = kwargs.get('llm_provider', 'openai').lower()
        resolved = kwargs.get('engine', '')

        provider_name = effective_provider

        # Return / create singleton interface
        cached = LLMFactory._llm_interfaces.get(provider_name)
        if cached is not None:
            return cached

        with LLMFactory._lock:
            cached2 = LLMFactory._llm_interfaces.get(provider_name)
            if cached2 is not None:
                return cached2

            LLM_class = get_llm_class(provider_name)
            if not LLM_class:
                raise ValueError(f"Unsupported LLM provider class: {provider_name}")

            llm_provider_instance = LLM_class(**kwargs)
            LLMFactory._llm_interfaces[provider_name] = LLMInterface(llm_provider_instance)

            logger.info(
                "LLMFactory: created interface provider='%s' engine='%s'",
                provider_name, resolved,
            )
            return LLMFactory._llm_interfaces[provider_name]


class Link(BaseModel):
    key: str
    value: str

class AgentForm(BaseModel):
    final_answer_content: str
    summary: str
    important_links: List[Link]
