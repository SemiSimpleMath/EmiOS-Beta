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
def _load_tier_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "configs" / "model_tiers.yaml"
    if not config_path.exists():
        logger.warning("model_tiers.yaml not found at %s — tier resolution disabled", config_path)
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_engine(provider: str, engine: str, model_tier: str | None) -> str:
    """Return the concrete model name to use.

    Priority:
    1. model_tier present → look up model for (tier, provider) in model_tiers.yaml
    2. engine present     → infer tier from engine_to_tier table, then resolve
                            to provider's equivalent (handles cross-provider remap)
    3. fallback           → return engine as-is
    """
    cfg = _load_tier_config()
    if not cfg:
        return engine

    tiers = cfg.get("tiers", {})
    engine_to_tier = cfg.get("engine_to_tier", {})

    tier = model_tier
    if not tier and engine:
        tier = engine_to_tier.get(engine)

    if not tier:
        return engine

    tier_cfg = tiers.get(tier)
    if not tier_cfg:
        logger.warning("resolve_engine: unknown tier '%s', using engine '%s' as-is", tier, engine)
        return engine

    resolved = tier_cfg.get(provider)
    if not resolved:
        logger.warning(
            "resolve_engine: no model for tier='%s' provider='%s', using engine '%s' as-is",
            tier, provider, engine,
        )
        return engine

    if resolved != engine:
        logger.debug(
            "resolve_engine: tier='%s' provider='%s' -> %s (was %s)",
            tier, provider, resolved, engine or "<none>",
        )
    return resolved


class LLMFactory:
    _llm_interfaces = {}  # Dict of provider_name -> LLMInterface singleton
    _lock = threading.Lock()

    @staticmethod
    def resolve_effective_params(**kwargs) -> dict:
        """Resolve the effective provider and engine after rerouting and tier mapping.

        Returns a copy of kwargs with llm_provider and engine set to what the
        actual API call will use.  Callers can use this to sync their params dict
        before calling structured_output().
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

        # Model tier resolution
        model_tier = kwargs.pop('model_tier', None)
        raw_engine = kwargs.get('engine', '')

        if effective_provider != requested_provider:
            env_model = os.environ.get("DEFAULT_LLM_MODEL", "").strip()
            if env_model:
                kwargs['engine'] = env_model
                raw_engine = env_model

        resolved = resolve_engine(effective_provider, raw_engine, model_tier)
        kwargs['engine'] = resolved
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
