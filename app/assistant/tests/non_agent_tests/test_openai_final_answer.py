"""The OpenAI client reads a structured result from the final-answer message only.

Reasoning models (gpt-6) can send a preamble as its own message (phase="commentary") before the
answer; `output_text` joins both. Messages are built from the SDK's own types so a field rename
(phase, refusal) fails here.
"""
from types import SimpleNamespace

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseReasoningItem

from app.services.llm_client import (
    OpenAIModelCapabilityNormalizer,
    _extract_final_answer_text,
    _parse_final_answer_json,
)


def _message(text, phase=None, part_type="output_text"):
    part = ({"type": "output_text", "text": text, "annotations": []} if part_type == "output_text"
            else {"type": "refusal", "refusal": text})
    data = {"id": "msg", "type": "message", "role": "assistant", "status": "completed", "content": [part]}
    if phase:
        data["phase"] = phase
    return ResponseOutputMessage.model_validate(data)


def _response(*items):
    return SimpleNamespace(output=list(items))


_REASONING = ResponseReasoningItem.model_validate({"id": "rs", "type": "reasoning", "summary": []})


def test_preamble_is_not_the_answer():
    response = _response(_REASONING, _message("I will check the evidence first.", "commentary"),
                         _message('{"verdict": "retry"}', "final_answer"))
    assert _parse_final_answer_json(response) == {"verdict": "retry"}


def test_a_model_without_phases_answers_in_its_one_message():
    assert _parse_final_answer_json(_response(_REASONING, _message('{"a": 1}'))) == {"a": 1}


def test_no_final_answer_raises():
    with pytest.raises(ValueError, match="0 final-answer messages"):
        _extract_final_answer_text(_response(_message("Working on it.", "commentary")))


def test_two_unlabelled_messages_raise():
    with pytest.raises(ValueError, match="0 final-answer messages"):
        _extract_final_answer_text(_response(_message('{"a": 1}'), _message('{"a": 2}')))


def test_refusal_raises():
    with pytest.raises(ValueError, match="refused"):
        _extract_final_answer_text(_response(_message("I can't help with that.", "final_answer", "refusal")))


def test_text_around_the_json_is_not_skipped():
    with pytest.raises(ValueError):
        _parse_final_answer_json(_response(_message('Here it is: {"a": 1}', "final_answer")))


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna", "gpt-6.1-sol"])
def test_reasoning_models_get_effort_and_no_temperature(model):
    kwargs = OpenAIModelCapabilityNormalizer(model).build_base_kwargs(messages=[], timeout=10, temperature=0.2)
    assert kwargs["reasoning"] == {"effort": "medium"}
    assert "temperature" not in kwargs


def test_other_models_keep_temperature():
    kwargs = OpenAIModelCapabilityNormalizer("gpt-4.1").build_base_kwargs(messages=[], timeout=10, temperature=0.2)
    assert kwargs["temperature"] == 0.2
    assert "reasoning" not in kwargs
