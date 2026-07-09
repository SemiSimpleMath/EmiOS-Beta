"""Telemetry integrity (LLM audit L3 + L5, 2026-07-09).

OpenAI's structured_output_json was a separate duplicated body with no
llm_call_log hook — dict-schema agents had zero telemetry rows ever. It
now delegates to structured_output (one path: telemetry, quota
translation, timeout ladder). And an engine missing from llm_prices.json
silently costed $0; it now warns once per engine.
"""
from __future__ import annotations

import json

from app.services import llm_client as lc


def test_structured_output_json_delegates_to_logged_path(monkeypatch):
    called = {}

    def _fake_structured_output(self, messages, **params):
        called["messages"] = messages
        called.update(params)
        return {"ok": True}

    monkeypatch.setattr(lc.OpenAILLM, "structured_output", _fake_structured_output)
    inst = lc.OpenAILLM.__new__(lc.OpenAILLM)  # no API call; delegation only
    out = inst.structured_output_json(
        [{"role": "user", "content": "hi"}],
        response_format={"type": "object", "properties": {}},
        engine="gpt-5-nano",
    )
    assert out == {"ok": True}
    assert called["engine"] == "gpt-5-nano"
    assert called["response_format"] == {"type": "object", "properties": {}}


def test_unpriced_engine_warns_once(monkeypatch):
    import app.services.llm_call_logger as lcl

    lcl._unpriced_warned.discard("test-engine-zzz")
    warned = []
    monkeypatch.setattr(
        lcl.logger, "warning",
        lambda msg, *a, **k: warned.append(msg % a if a else str(msg)),
    )
    assert lcl._cost_for("test-engine-zzz", 1000, 10) == (0.0, 0.0)
    lcl._cost_for("test-engine-zzz", 1000, 10)
    lcl._cost_for("test-engine-zzz", 1000, 10)
    assert len([w for w in warned if "test-engine-zzz" in w]) == 1
    lcl._unpriced_warned.discard("test-engine-zzz")


def test_priced_engine_does_not_warn(monkeypatch):
    import app.services.llm_call_logger as lcl

    warned = []
    monkeypatch.setattr(lcl.logger, "warning", lambda *a, **k: warned.append(a))
    in_cost, out_cost = lcl._cost_for("gpt-5-mini", 1_000_000, 1_000_000)
    assert (in_cost, out_cost) == (0.25, 2.00)
    assert warned == []


def test_tier_anthropic_models_are_priced():
    """model_tiers.yaml routes anthropic tiers at claude-3-5-* — those
    engines must have price entries or their cost records $0 (audit L5)."""
    prices = json.load(open("configs/llm_prices.json", encoding="utf-8"))
    for engine in ("claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"):
        assert engine in prices, f"{engine} missing from llm_prices.json"
        assert prices[engine]["input_per_1m_usd"] > 0
