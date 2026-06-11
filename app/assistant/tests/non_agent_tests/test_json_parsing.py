"""parse_jsonish — the consolidated LLM/MCP-output JSON extractor
(duplicate audit 2026-06-10). Covers every strategy the nine retired
copies implemented, so their call sites keep working unchanged."""
from __future__ import annotations

from app.assistant.utils.json_parsing import parse_jsonish


def test_raw_json():
    assert parse_jsonish('{"a": 1}') == {"a": 1}
    assert parse_jsonish('[1, 2]') == [1, 2]
    assert parse_jsonish('  {"a": 1}  ') == {"a": 1}


def test_leading_json_with_trailing_prose():
    assert parse_jsonish('{"a": 1} and some trailing text') == {"a": 1}


def test_fenced_json_block():
    assert parse_jsonish('Here:\n```json\n{"a": 1}\n```\ndone') == {"a": 1}
    assert parse_jsonish('```\n[1, 2]\n```') == [1, 2]


def test_fenced_json_with_trailing_prose_inside_fence():
    # raw_decode retry inside the fence payload
    assert parse_jsonish('```json\n{"a": 1} trailing\n```') == {"a": 1}


def test_markdown_header_then_json():
    # The Playwright MCP wrapper format: "### Result\n{...}\n### Ran ..."
    text = '### Result\n{"a": 1}\n### Ran Playwright code\n```js\nfoo()\n```'
    assert parse_jsonish(text) == {"a": 1}


def test_json_embedded_mid_prose():
    # structure scan: every '{'/'[' position is tried
    assert parse_jsonish('The answer is {"a": [1, {"b": 2}]} ok?') == {"a": [1, {"b": 2}]}


def test_js_fence_before_json_fence_is_skipped():
    text = '```js\nnot json (\n```\nthen ```json\n{"a": 1}\n```'
    assert parse_jsonish(text) == {"a": 1}


def test_garbage_returns_none():
    assert parse_jsonish("no json here at all") is None
    assert parse_jsonish("") is None
    assert parse_jsonish("   ") is None
    assert parse_jsonish(None) is None
    assert parse_jsonish(42) is None


def test_scalar_json_values():
    assert parse_jsonish("true") is True
    assert parse_jsonish("3") == 3
