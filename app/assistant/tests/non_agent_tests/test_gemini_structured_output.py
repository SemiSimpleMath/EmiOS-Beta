"""Gemini structured-output caching fix (2026-06-13).

The old code prepended a unique uuid to every prompt to dodge an
intermittent "infinite-loop JSON" degeneration on implicit-cache hits —
which defeated caching on every call (proven via scratch/gemini_cache_repro.py:
cached=0 with the nonce, cached=8027 without). The fix sanitizes the schema,
removes the blanket nonce (so caching engages), and retries the rare
degenerate response WITH a cache-bust nonce as a surgical recovery.

These cover the pure logic + the retry loop with a fake client (no live calls).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import llm_client as L


# ── schema sanitizers ──────────────────────────────────────────────────────


def test_inline_refs_flattens_defs():
    schema = {
        "$defs": {"Item": {"type": "object", "properties": {"n": {"type": "string"}}}},
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}},
    }
    out = L._gemini_inline_refs(schema)
    assert "$defs" not in out
    assert out["properties"]["items"]["items"] == {"type": "object", "properties": {"n": {"type": "string"}}}


def test_inline_refs_breaks_recursive_cycle():
    # A self-referential model must not hang the inliner.
    schema = {
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }
    out = L._gemini_inline_refs(schema)  # must terminate
    # The cycle is cut to an open object rather than recursing forever.
    assert out["properties"]["root"]["properties"]["child"] == {"type": "object"}


def test_clean_schema_strips_unsupported_keys_keeps_property_names():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AgentForm",
        "properties": {
            # 'title' here is a PROPERTY NAME and must survive
            "title": {"type": "string", "title": "Title"},
            "score": {"type": "number", "title": "Score"},
        },
    }
    out = L._gemini_clean_schema(schema)
    assert "additionalProperties" not in out
    assert "$schema" not in out
    assert "title" in out["properties"]                 # property name kept
    assert "title" not in out["properties"]["title"]    # metadata title stripped


# ── degenerate-response detector ────────────────────────────────────────────


def _resp(text, finish="STOP", block=None):
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish, safety_ratings=None)],
        prompt_feedback=SimpleNamespace(block_reason=block),
    )


def test_check_response_clean_parse():
    import json
    reason, parsed = L.GeminiLLM._check_structured_response(
        None, _resp('{"a": 1}'), "m", json)
    assert reason is None and parsed == {"a": 1}


def test_check_response_empty_is_degenerate():
    import json
    reason, parsed = L.GeminiLLM._check_structured_response(
        None, _resp("", finish="SAFETY", block="SAFETY"), "m", json)
    assert reason is not None and "empty" in reason and parsed is None


def test_check_response_max_tokens_is_degenerate():
    import json
    reason, parsed = L.GeminiLLM._check_structured_response(
        None, _resp('{"a": 1, "a": 1, "a"', finish="MAX_TOKENS"), "m", json)
    assert reason == "max_tokens_truncation" and parsed is None


def test_check_response_bad_json_is_degenerate():
    import json
    reason, parsed = L.GeminiLLM._check_structured_response(
        None, _resp("not json {{{"), "m", json)
    assert reason is not None and "json_parse_error" in reason and parsed is None


# ── retry loop (fake client, no live calls) ─────────────────────────────────


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        return self._responses.pop(0)


def _make_gemini(responses):
    from google.genai import types
    g = object.__new__(L.GeminiLLM)  # bypass __new__/_init_once (no API key needed)
    g.types = types
    g.engine = "gemini-3-flash-preview"
    g.temperature = 0.1
    g.client = SimpleNamespace(models=_FakeModels(responses))
    return g


_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}


def _usage():
    return SimpleNamespace(prompt_token_count=10, cached_content_token_count=8,
                           candidates_token_count=5, thoughts_token_count=0,
                           total_token_count=15)


def test_retry_adds_nonce_only_after_a_degenerate_response(monkeypatch):
    import app.services.llm_call_logger as logmod
    monkeypatch.setattr(logmod, "record_llm_call", lambda **k: None)

    bad = _resp("not json{", finish="STOP"); bad.usage_metadata = _usage()
    good = _resp('{"verdict": "ok"}'); good.usage_metadata = _usage()
    g = _make_gemini([bad, good])

    result = g.structured_output(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
        response_format=_SCHEMA, engine="gemini-3-flash-preview", temperature=0.1,
    )
    assert result == {"verdict": "ok"}
    prompts = g.client.models.prompts
    assert len(prompts) == 2
    assert not prompts[0].startswith("[req-")   # happy path: no nonce -> caching
    assert prompts[1].startswith("[req-")        # retry: cache-bust nonce applied


def test_persistent_degenerate_fails_loud(monkeypatch):
    import app.services.llm_call_logger as logmod
    monkeypatch.setattr(logmod, "record_llm_call", lambda **k: None)

    bad = _resp("nope", finish="MAX_TOKENS"); bad.usage_metadata = _usage()
    g = _make_gemini([bad, bad, bad])

    with pytest.raises(ValueError, match="degenerate after 3 attempts"):
        g.structured_output(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
            response_format=_SCHEMA, engine="gemini-3-flash-preview", temperature=0.1,
        )
    assert len(g.client.models.prompts) == 3  # exhausted all attempts
