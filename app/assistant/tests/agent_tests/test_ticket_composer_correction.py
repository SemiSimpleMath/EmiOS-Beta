"""The ticket composer's answer refused by its form gets one correction (2026-10-13).

The question whether the owner raised fatigue at his physical was never sent: the composer labelled a choice "Help me
follow up", its form refused a label over 3 words, and the manager was left with no composer output. Pins: a form
refusal is asked once more with the refusal named after the same prompt; a second refusal raises; a failure that is
not the form's is not retried. Fake model calls; no manager, no database.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.assistant.agent_classes.TicketComposer import TicketComposer
from app.assistant.agents.ticket_builder.composer.agent_form import AgentForm

GOOD = {"reasoning": "A question needs a reply.", "ticket_kind": "decision", "suggestion_type": "wellness",
        "title": "Fatigue at your physical", "message": "Did you bring up fatigue and sleep at your physical?",
        "response_choices": [{"label": "Yes", "meaning": "yes", "scope": "He raised it."},
                             {"label": "Follow up", "meaning": "no", "scope": "He did not; he wants help to follow up."}]}


def _refused() -> RuntimeError:
    bad = {**GOOD, "response_choices": [GOOD["response_choices"][0],
                                        {**GOOD["response_choices"][1], "label": "Help me follow up"}]}
    try:
        AgentForm.model_validate(bad)
    except ValidationError as e:
        try:
            raise RuntimeError(f"LLM structured_output failed: {e}") from e
        except RuntimeError as r:
            return r
    raise AssertionError("the form accepted a label over 3 words")


def _composer(answers):
    composer = object.__new__(TicketComposer)
    composer.name, composer.config, calls = "ticket_builder::composer", {"structured_output": {"type": "object"}}, []

    def call_llm(messages, response_format, use_json):
        calls.append(messages)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    composer.call_llm = call_llm
    return composer, calls


def test_a_refused_label_is_named_and_the_corrected_answer_is_kept():
    composer, calls = _composer([_refused(), GOOD])
    prompt = [{"role": "system", "content": "compose"}, {"role": "user", "content": "the brief"}]
    assert composer._execute_model(prompt) == GOOD
    assert len(calls) == 2 and calls[1][:2] == prompt
    assert "YOUR PREVIOUS ANSWER WAS REFUSED" in calls[1][2]["content"]
    assert "response_choices.1.label: Value error, Labels require at most 3 words" in calls[1][2]["content"], \
        "the refusal names what was wrong"


def test_a_second_refusal_raises():
    composer, calls = _composer([_refused(), _refused()])
    with pytest.raises(RuntimeError, match="structured_output failed"):
        composer._execute_model([{"role": "user", "content": "the brief"}])
    assert len(calls) == 2


def test_a_failure_that_is_not_the_form_s_is_not_retried():
    composer, calls = _composer([RuntimeError("LLM structured_output failed: connection reset")])
    with pytest.raises(RuntimeError, match="connection reset"):
        composer._execute_model([{"role": "user", "content": "the brief"}])
    assert len(calls) == 1
