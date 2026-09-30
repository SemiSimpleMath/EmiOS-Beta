"""LocalLLM — a model on this machine, schema ENFORCED by the server.

No model runs here: the OpenAI client is replaced by a fake that records the request and
returns a canned completion. Pins the contract: the Pydantic form goes out as a strict
json_schema response format, the reply is validated with the form, and anything that is not
the form's shape fails loudly. The live proof against a real local model is
agent_tests/local_llm/run_local_structured.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import List, Literal

import pytest
from pydantic import BaseModel

from app.services.llm_client import LocalLLM


class Verdict(BaseModel):
    reasoning: str
    verdict: Literal["same", "refines", "contradicts", "new"]
    sources: List[int]


class _FakeCompletions:
    def __init__(self, content, finish_reason="stop", prompt_tokens=100):
        self.content, self.finish_reason, self.calls = content, finish_reason, []
        self.prompt_tokens = prompt_tokens

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=self.prompt_tokens), choices=[SimpleNamespace(
            finish_reason=self.finish_reason, message=SimpleNamespace(content=self.content))])


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(LocalLLM, "_instance", None)
    p = LocalLLM(engine="gemma4:12b")
    monkeypatch.setattr("app.services.llm_call_logger.record_llm_call", lambda **kw: None)
    return p


def _use(provider, content, finish_reason="stop", prompt_tokens=100):
    fake = _FakeCompletions(content, finish_reason, prompt_tokens)
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return fake


MSGS = [{"role": "user", "content": "judge"}]


def test_the_form_is_sent_as_an_enforced_schema_and_the_reply_validated(provider):
    fake = _use(provider, '{"reasoning": "r", "verdict": "refines", "sources": [1]}')
    out = provider.structured_output(MSGS, response_format=Verdict, timeout=30)
    assert out == {"reasoning": "r", "verdict": "refines", "sources": [1]}
    rf = fake.calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == Verdict.model_json_schema()
    assert fake.calls[0]["model"] == "gemma4:12b"


def test_a_reply_outside_the_form_fails_loudly(provider):
    _use(provider, '{"reasoning": "r", "verdict": "maybe", "sources": [1]}')
    with pytest.raises(RuntimeError, match="validation error"):
        provider.structured_output(MSGS, response_format=Verdict, timeout=30)


def test_prose_or_empty_is_never_scanned_for_json(provider):
    _use(provider, 'Sure! {"reasoning": "r", "verdict": "new", "sources": []}')
    with pytest.raises(RuntimeError, match="non-JSON"):
        provider.structured_output(MSGS, response_format=Verdict, timeout=30)
    _use(provider, "", finish_reason="length")
    with pytest.raises(RuntimeError, match="empty completion"):
        provider.structured_output(MSGS, response_format=Verdict, timeout=30)


def test_multimodal_content_is_refused(provider):
    _use(provider, "{}")
    with pytest.raises(ValueError, match="text-only"):
        provider.structured_output([{"role": "user", "content": [{"type": "image_url"}]}],
                                   response_format=Verdict, timeout=30)


def test_registered_and_configured_by_base_url(monkeypatch):
    from app.configs.llm_classes_dict import _key_is_present, get_llm_class
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    assert not _key_is_present("local")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert _key_is_present("local") and get_llm_class("local") is LocalLLM


def test_a_prompt_the_server_would_cut_raises(provider, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_CONTEXT_TOKENS", "32768")
    _use(provider, '{"reasoning": "r", "verdict": "new", "sources": []}', prompt_tokens=16386)
    with pytest.raises(RuntimeError, match="prompt truncated"):
        provider.structured_output(MSGS, response_format=Verdict, timeout=30, max_tokens=16384)
    _use(provider, '{"reasoning": "r", "verdict": "new", "sources": []}', prompt_tokens=9000)
    assert provider.structured_output(MSGS, response_format=Verdict, timeout=30, max_tokens=16384)["verdict"] == "new"
