"""Prompt-assembly guards (fragility review #3, 2026-06-12).

Blank input does not look like an error to an LLM — it looks like
conservative judgment. Four judgment agents shipped judging empty input
with zero errors. Three guard layers now refuse loudly at the template
boundary:

  1. required_context_items — agent declares judgment-critical items;
     empty resolution refuses invocation
  2. skeleton check — rendered prompt identical to the all-empty-context
     render means NO data reached the template
  3. strict_template — Jinja StrictUndefined per agent (typos raise)
"""
import os

os.environ["USE_TEST_DB"] = "true"
os.environ["TEST_DB_NAME"] = "test_prompt_guards"

import app.assistant.tests.test_setup  # noqa: F401

from unittest.mock import patch

import pytest

from app.assistant.agent_runtime.exceptions import PromptRenderError
from app.assistant.agent_runtime.services.llm_client import LLMClient
from app.assistant.agent_runtime.services.prompt_builder import (
    _jinja_env,
    _strict_jinja_env,
    enforce_required_context_items,
    enforce_skeleton_guard,
    get_jinja_env_for_agent,
)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


# ── layer 1: required_context_items ─────────────────────────────────────


def test_required_items_refuse_empty_values():
    for empty in (None, "", "   ", [], {}):
        with pytest.raises(PromptRenderError, match="agent_input"):
            enforce_required_context_items(
                "test_agent", {"agent_input": empty}, ["agent_input"])
    # Missing key entirely is also empty.
    with pytest.raises(PromptRenderError):
        enforce_required_context_items("test_agent", {}, ["agent_input"])


def test_required_items_pass_real_values():
    enforce_required_context_items(
        "test_agent",
        {"agent_input": {"label": "x"}, "task": "do things"},
        ["agent_input", "task"],
    )
    enforce_required_context_items("test_agent", {"anything": None}, None)
    enforce_required_context_items("test_agent", {"anything": None}, [])


# ── layer 2: skeleton check ──────────────────────────────────────────────

TPL = "## Entity\nLabel: {{ label }}\nEra: {{ start_date }}\n"


def test_skeleton_guard_raises_when_no_data_reached_template():
    skeleton_equal = _jinja_env.from_string(TPL).render().replace("\n\n", "\n")
    with pytest.raises(PromptRenderError, match="skeleton"):
        enforce_skeleton_guard("test_agent", TPL, skeleton_equal)


def test_skeleton_guard_passes_with_data():
    rendered = _jinja_env.from_string(TPL).render(
        label="Tom's House", start_date="2010").replace("\n\n", "\n")
    enforce_skeleton_guard("test_agent", TPL, rendered)


def test_skeleton_guard_exempts_static_templates():
    static = "Judge the thing. Answer in JSON."
    enforce_skeleton_guard("test_agent", static, static)


# ── layer 3: strict_template ─────────────────────────────────────────────


def test_strict_env_selected_by_config_flag():
    class A:
        config = {"strict_template": True}

    class B:
        config = {}

    assert get_jinja_env_for_agent(A()) is _strict_jinja_env
    assert get_jinja_env_for_agent(B()) is _jinja_env


def test_strict_env_raises_on_undefined_variable():
    from jinja2 import UndefinedError
    tpl = _strict_jinja_env.from_string("Label: {{ lable }}")  # typo
    with pytest.raises(UndefinedError):
        tpl.render(label="x")
    # Lenient env silently renders empty — the legacy behavior.
    assert _jinja_env.from_string("Label: {{ lable }}").render(label="x") == "Label: "


# ── end to end: the original incident can no longer happen ──────────────


def test_judgment_agent_refuses_empty_agent_input():
    """The 2026-06-12 incident shape: a judgment agent invoked with no
    usable agent_input must refuse loudly, not judge a blank prompt."""
    agent = DI.agent_factory.create_agent("kg_maintenance::succession_judge")
    assert agent is not None

    def no_llm(self, *, agent, messages, response_format=None, use_json=False):
        raise AssertionError("LLM must not be called on empty input")

    with patch.object(LLMClient, "call_structured_output", no_llm):
        with pytest.raises(Exception) as exc_info:
            agent.action_handler(Message(agent_input=None))
    assert "required context item" in str(exc_info.value)
