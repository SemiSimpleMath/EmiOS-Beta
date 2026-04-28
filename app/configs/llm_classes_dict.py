import os
from app.services.llm_client import OpenAILLM, GeminiLLM, AnthropicLLM

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

# LLM dictionary containing different LLM providers, their class, and default parameters
LLM_CLASSES = {
    "openai": {
        "class": OpenAILLM,
        "params": {
            "engine": os.getenv("DEFAULT_LLM_MODEL", "gpt-5.4-mini"),
            "temperature": 0.1,
        }
    },
    "gemini": {
        "class": GeminiLLM,
        "params": {
            "engine": os.getenv("DEFAULT_LLM_MODEL", "gemini-3-flash-preview"),
            "temperature": 0.1,
        }
    },
    "anthropic": {
        "class": AnthropicLLM,
        "params": {
            "engine": os.getenv("DEFAULT_LLM_MODEL", "claude-3-5-haiku-20241022"),
            "temperature": 0.1,
        }
    },
}

# Environment variable key names per provider (used for key-presence checks)
_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# Default models per provider (used when remapping to default provider).
# Keep in sync with the "mini" tier in configs/model_tiers.yaml.
_PROVIDER_DEFAULT_MODEL = {
    "openai": "gpt-5.4-mini",
    "gemini": "gemini-3-flash-preview",
    "anthropic": "claude-3-5-haiku-20241022",
}


def _key_is_present(provider_name: str) -> bool:
    env_var = _PROVIDER_KEY_ENV.get(provider_name)
    if not env_var:
        return False
    val = os.environ.get(env_var, "").strip().lower()
    if not val:
        return False
    placeholders = {"dummy_api_key", "your_api_key_here", "change_me", "changeme", "none", "null", "sk-..."}
    return val not in placeholders


def get_llm_class(provider_name: str):
    """Retrieve the LLM class based on provider name.

    If the requested provider has no valid API key but DEFAULT_LLM_PROVIDER is
    configured with a valid key, the request is transparently rerouted to that
    provider so that agents with hardcoded provider names still function.
    """
    provider_name = provider_name.lower()

    default_provider = os.environ.get("DEFAULT_LLM_PROVIDER", "").strip().lower()

    if (
        default_provider
        and default_provider != provider_name
        and not _key_is_present(provider_name)
        and _key_is_present(default_provider)
    ):
        logger.info(
            "Provider '%s' has no valid key; rerouting to DEFAULT_LLM_PROVIDER='%s'",
            provider_name,
            default_provider,
        )
        provider_name = default_provider

    llm_entry = LLM_CLASSES.get(provider_name)
    if not llm_entry:
        logger.error("Unsupported LLM provider: %s", provider_name)
        return None

    return llm_entry["class"]


def get_llm_class_params(provider_name: str):
    """Retrieve the default parameters for an LLM provider."""
    provider_name = provider_name.lower()
    llm_entry = LLM_CLASSES.get(provider_name)

    if not llm_entry:
        logger.error("Unsupported LLM provider: %s", provider_name)
        return None

    return llm_entry["params"]


def get_default_engine_for_provider(provider_name: str) -> str:
    """Return the default model name for a given provider."""
    return _PROVIDER_DEFAULT_MODEL.get(provider_name.lower(), "gpt-4.1-mini")
