"""Prompts reach the provider VERBATIM — no ASCII flattening.

Until 2026-07-08 every non-Gemini prompt passed through normalize_to_ascii:
diacritics decomposed ("mökki" -> "mokki", "Päivi" -> "Paivi") and anything
untranslatable (emoji, CJK) was silently DELETED — the model never saw the
user's actual words (context-injection audit C1). This pins the round trip
end-to-end through PromptBuilder.construct_prompt for an openai-provider
agent (the previously-mangled path).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.assistant.agent_runtime.services.context_injector import ContextInjector
from app.assistant.agent_runtime.services.entity_injector import EntityInjector
from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message

FINNISH_SYSTEM = "Sinä olet ystävällinen avustaja. Hyvää yötä!"
FINNISH_INPUT = "Nähdään mökillä lauantaina! 🌲 – Päivi & Mikko"


def _stub_agent():
    return SimpleNamespace(
        name="unicode_probe",
        config={
            "prompts": {
                "system": FINNISH_SYSTEM,
                "user": "Käyttäjä sanoo: {{ incoming_message }}",
            },
            "user_context_items": [],
            "system_context_items": [],
        },
        llm_params={"llm_provider": "openai"},
        blackboard=Blackboard(),
        components=SimpleNamespace(
            context_injector=ContextInjector(),
            entity_injector=EntityInjector(),
        ),
    )


def test_finnish_text_survives_prompt_build_verbatim():
    agent = _stub_agent()
    message = Message(agent_input=FINNISH_INPUT)

    msgs = PromptBuilder().construct_prompt(agent, message, entity_injection_keys=set())

    system_text = msgs[0]["content"]
    user_text = msgs[1]["content"]
    assert isinstance(user_text, str)

    # Diacritics intact — not decomposed to bare ASCII.
    assert "Sinä olet ystävällinen avustaja. Hyvää yötä!" in system_text
    assert "mokki" not in user_text and "Paivi" not in user_text

    # The user's words verbatim: umlauts, emoji, en dash, ampersand.
    assert FINNISH_INPUT in user_text
