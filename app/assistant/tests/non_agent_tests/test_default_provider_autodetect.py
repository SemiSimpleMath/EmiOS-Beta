"""Single-provider auto-detection covers EVERY registered provider.

_auto_detect_default_llm_provider picks DEFAULT_LLM_PROVIDER when exactly one
provider has a usable key. It used to scan a hardcoded
("openai", "gemini", "anthropic") tuple that silently omitted opencode, so an
install whose only credential was OPENCODE_API_KEY fell into the "nothing
configured" branch: it logged "No LLM provider API keys found. Agents will fail
on LLM calls." and left DEFAULT_LLM_PROVIDER unset — while a perfectly usable
provider sat right there in the environment.

The candidate list is now derived from _PROVIDER_KEY_ENV (the provider registry
in llm_classes_dict) instead of being repeated literally, so these tests are
really asserting that the two never drift apart again.
"""
from __future__ import annotations

import os

import pytest

from app.bootstrap import _auto_detect_default_llm_provider
from app.configs.llm_classes_dict import _PROVIDER_KEY_ENV, _PROVIDER_DEFAULT_MODEL


@pytest.fixture
def clean_env(monkeypatch):
    """No provider keys and no pre-set default, so each test starts neutral."""
    monkeypatch.delenv("DEFAULT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_LLM_MODEL", raising=False)
    for env_var in _PROVIDER_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    return monkeypatch


@pytest.mark.parametrize("provider", sorted(_PROVIDER_KEY_ENV))
def test_sole_provider_is_detected(clean_env, provider):
    """Every registered provider — not just the original three — is detectable."""
    clean_env.setenv(_PROVIDER_KEY_ENV[provider], f"test-key-for-{provider}")

    _auto_detect_default_llm_provider()

    assert os.environ.get("DEFAULT_LLM_PROVIDER") == provider
    assert os.environ.get("DEFAULT_LLM_MODEL") == _PROVIDER_DEFAULT_MODEL[provider]


def test_opencode_only_install_is_not_reported_as_unconfigured(clean_env):
    """The exact regression: opencode was the one provider that fell through."""
    clean_env.setenv("OPENCODE_API_KEY", "oc_gol_testkey")

    _auto_detect_default_llm_provider()

    assert os.environ.get("DEFAULT_LLM_PROVIDER") == "opencode"


def test_explicit_choice_is_never_overridden(clean_env):
    """An operator who named a provider outranks whatever keys happen to exist."""
    clean_env.setenv("DEFAULT_LLM_PROVIDER", "opencode")
    clean_env.setenv("OPENAI_API_KEY", "sk-test")

    _auto_detect_default_llm_provider()

    assert os.environ.get("DEFAULT_LLM_PROVIDER") == "opencode"


def test_multiple_providers_leaves_choice_to_the_resolver(clean_env):
    """Ambiguous: don't guess. The factory's rerouting layer decides instead."""
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    clean_env.setenv("OPENCODE_API_KEY", "oc_gol_testkey")

    _auto_detect_default_llm_provider()

    assert os.environ.get("DEFAULT_LLM_PROVIDER") is None


def test_no_keys_leaves_default_unset(clean_env):
    _auto_detect_default_llm_provider()

    assert os.environ.get("DEFAULT_LLM_PROVIDER") is None


@pytest.mark.parametrize(
    "placeholder",
    [
        "your_api_key_here",           # literal sentinel from .env.example
        "sk-...",                      # ditto, OpenAI-shaped
        "dummy_api_key",
        "change_me",
        "your_opencode_api_key_here",  # per-provider variant — prefix rule
        "placeholder-key",
        "sk-placeholder",
        "<paste-your-key>",
    ],
)
def test_placeholder_key_does_not_count_as_configured(clean_env, placeholder):
    """Scaffolding values must not select a provider.

    _key_is_present tests an exact set AND a set of prefixes. The prefixes are
    what catch hand-written per-provider variants like
    "your_opencode_api_key_here"; an exact-match-only rule read those as real
    keys, so a half-finished .env could auto-select a provider whose key was
    never actually filled in. That surfaced later as a puzzling 401 from the
    provider rather than an honest "no key configured".
    """
    clean_env.setenv("OPENCODE_API_KEY", placeholder)

    _auto_detect_default_llm_provider()

    assert os.environ.get("DEFAULT_LLM_PROVIDER") is None
