"""A None context value renders as '' — never the literal string "None".

The generic context fall-through resolves undeclared keys to None, and
Jinja's default finalize prints None as "None" — so a prep-node miss put
the word "None" into prompts across the 137 templates that bare-render
optional keys (context-injection audit C4). The shared envs now carry a
finalize that blanks None; undefined variables keep their semantics
(non-strict blank, strict raises).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from jinja2 import UndefinedError

from app.assistant.agent_runtime.services.context_injector import ContextInjector
from app.assistant.agent_runtime.services.entity_injector import EntityInjector
from app.assistant.agent_runtime.services.prompt_builder import (
    PromptBuilder,
    _jinja_env,
    _strict_jinja_env,
)
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message


def test_none_value_renders_blank_in_default_env():
    assert _jinja_env.from_string("A:{{ x }};B:{{ y }}").render(x=None, y="ok") == "A:;B:ok"


def test_none_value_renders_blank_in_strict_env():
    assert _strict_jinja_env.from_string("A:{{ x }}").render(x=None) == "A:"


def test_strict_env_still_raises_on_undefined():
    with pytest.raises(UndefinedError):
        _strict_jinja_env.from_string("{{ never_defined }}").render()


def test_fall_through_key_never_prints_none_end_to_end():
    agent = SimpleNamespace(
        name="none_probe",
        config={
            "prompts": {
                "system": "You are a probe.",
                "user": "Msg: {{ incoming_message }} | Optional: {{ some_optional_key }}!",
            },
            "user_context_items": ["some_optional_key"],
            "system_context_items": [],
        },
        llm_params={"llm_provider": "openai"},
        blackboard=Blackboard(),  # some_optional_key resolves to None via fall-through
        components=SimpleNamespace(
            context_injector=ContextInjector(),
            entity_injector=EntityInjector(),
        ),
    )
    rendered = PromptBuilder().get_user_prompt(agent, Message(agent_input="hello"))
    assert "None" not in rendered
    assert "Msg: hello | Optional: !" in rendered
