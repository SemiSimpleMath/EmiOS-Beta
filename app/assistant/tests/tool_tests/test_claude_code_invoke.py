"""Unit tests for the claude_code_invoke helpers.

Covers the parts that don't need DI / Flask / a real ``claude`` binary:
  - stream-json parsing from a captured stdout buffer
  - skill curator: keyword extraction, doc matching, file capping
  - session store: round-trip across files
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from app.assistant.lib.tools.claude_code_invoke import (
    coding_agent_runner,
    session_store,
    skill_curator,
)


# ─── coding_agent_runner._parse_stream ────────────────────────────────


def test_parse_stream_extracts_session_id_and_final_text():
    stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-abc-123"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello, working on it."}]},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Here's the answer: 42."}]},
        }),
        json.dumps({"type": "result", "result": "Final: 42."}),
    ])
    events, final, sid = coding_agent_runner._parse_stream(stdout)
    assert sid == "sess-abc-123"
    assert final == "Final: 42."
    assert len(events) == 4


def test_parse_stream_falls_back_to_last_assistant_when_no_result_event():
    stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-xyz"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Only assistant text."}]},
        }),
    ])
    events, final, sid = coding_agent_runner._parse_stream(stdout)
    assert sid == "sess-xyz"
    assert final == "Only assistant text."


def test_parse_stream_skips_malformed_lines_without_raising():
    stdout = "\n".join([
        json.dumps({"type": "system", "session_id": "sess-1"}),
        "not-json-at-all",
        "",
        json.dumps({"type": "result", "result": "ok"}),
    ])
    events, final, sid = coding_agent_runner._parse_stream(stdout)
    assert sid == "sess-1"
    assert final == "ok"
    # 2 valid JSON events parsed; the bad line and the empty line are skipped.
    assert len(events) == 2


def test_parse_stream_handles_empty_stdout():
    events, final, sid = coding_agent_runner._parse_stream("")
    assert events == []
    assert final == ""
    assert sid is None


def test_parse_stream_ignores_non_text_content_blocks():
    stdout = "\n".join([
        json.dumps({"type": "system", "session_id": "s"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "text", "text": "After tool use."},
            ]},
        }),
    ])
    _, final, _ = coding_agent_runner._parse_stream(stdout)
    assert final == "After tool use."


# ─── coding_agent_runner.run with mocked subprocess ──────────────────


def test_run_returns_failure_when_cli_not_found(monkeypatch):
    monkeypatch.setattr(coding_agent_runner, "find_cli", lambda *_a, **_k: None)
    result = coding_agent_runner.run(prompt="hi", cli_path="claude")
    assert result.success is False
    assert "not found" in result.error.lower()
    assert result.session_id is None


def test_run_passes_resume_flag_when_session_id_provided(monkeypatch):
    monkeypatch.setattr(coding_agent_runner, "find_cli", lambda *_a, **_k: "/fake/claude")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "result", "result": "ok"}),
            stderr="",
        )

    monkeypatch.setattr(coding_agent_runner.subprocess, "run", fake_run)
    coding_agent_runner.run(prompt="continue", session_id="sess-prev")
    assert "--resume" in captured["args"]
    idx = captured["args"].index("--resume")
    assert captured["args"][idx + 1] == "sess-prev"
    assert captured["input"] == "continue"


def test_run_surfaces_nonzero_exit_with_stderr(monkeypatch):
    monkeypatch.setattr(coding_agent_runner, "find_cli", lambda *_a, **_k: "/fake/claude")
    monkeypatch.setattr(coding_agent_runner.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=2, stdout="", stderr="auth failed",
    ))
    result = coding_agent_runner.run(prompt="hi")
    assert result.success is False
    assert "exit code 2" in result.error
    assert "auth failed" in result.error


# ─── skill_curator ─────────────────────────────────────────────────


def test_extract_keywords_drops_stopwords_and_short_tokens():
    kws = skill_curator._extract_keywords("How do I add a feature to dayflow_orchestrator?")
    # stopwords removed, length filter (>=3 chars), lowercased
    assert "dayflow_orchestrator" in kws
    assert "feature" in kws
    assert "the" not in kws
    assert "add" not in kws  # explicitly stopword


def test_extract_keywords_handles_empty():
    assert skill_curator._extract_keywords("") == set()


def test_topic_matched_docs_finds_matches_by_filename(tmp_path):
    arch = tmp_path / "docs" / "architecture"
    arch.mkdir(parents=True)
    (arch / "00_OVERVIEW.md").write_text("# Overview", encoding="utf-8")
    (arch / "05_DAYFLOW.md").write_text("# Dayflow Orchestrator\n\nRuns the autonomous loop.", encoding="utf-8")
    (arch / "10_PODS.md").write_text("# Pods\n\nAddressable content units.", encoding="utf-8")

    results = skill_curator._topic_matched_docs(
        tmp_path, {"dayflow", "feature"}, cap=3, exclude=set(),
    )
    paths = [p.name for p, _ in results]
    assert "05_DAYFLOW.md" in paths


def test_read_capped_truncates_oversize_files(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * 50_000, encoding="utf-8")
    out = skill_curator._read_capped(big)
    assert len(out) < 50_000
    assert "truncated" in out


# ─── session_store ─────────────────────────────────────────────────


def test_session_store_roundtrip(tmp_path, monkeypatch):
    fake_root = tmp_path
    monkeypatch.setattr(session_store, "get_repo_root", lambda: fake_root)
    assert session_store.get_session_id("emi_code_room") is None
    session_store.set_session_id("emi_code_room", "main", "sess-abc")
    assert session_store.get_session_id("emi_code_room") == "sess-abc"
    session_store.clear_session("emi_code_room")
    assert session_store.get_session_id("emi_code_room") is None


def test_session_store_handles_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "get_repo_root", lambda: tmp_path)
    p = tmp_path / "data" / "emi_code" / "sessions.json"
    p.parent.mkdir(parents=True)
    p.write_text("not json {", encoding="utf-8")
    # Corrupt file should not raise — we get None back and can re-save.
    assert session_store.get_session_id("emi_code_room") is None
    session_store.set_session_id("emi_code_room", "main", "fresh")
    assert session_store.get_session_id("emi_code_room") == "fresh"


# ─── /clear command path (no LLM, no subprocess) ─────────────────


def test_clear_command_clears_session_and_returns_confirmation(tmp_path, monkeypatch):
    """`/clear` short-circuits the tool: drop session, return confirmation, no subprocess call."""
    from app.assistant.lib.tools.claude_code_invoke.claude_code_invoke import ClaudeCodeInvoke
    from app.assistant.utils.pydantic_classes import ToolMessage

    monkeypatch.setattr(session_store, "get_repo_root", lambda: tmp_path)
    session_store.set_session_id("emi_code_room", "main", "sess-existing")

    # Make sure the runner is NOT called — mock it to fail loudly if it is.
    def _should_not_call(*a, **k):
        raise AssertionError("runner.run should not be invoked on /clear")
    monkeypatch.setattr(coding_agent_runner, "run", _should_not_call)

    tool = ClaudeCodeInvoke()
    msg = ToolMessage(
        tool_name="claude_code_invoke",
        tool_data={"tool_name": "claude_code_invoke", "arguments": {"task": "/clear"}},
        request_id="test",
        metadata={"room_id": "emi_code_room", "room_context_id": "main"},
    )
    result = tool.execute(msg)
    assert result.result_type == "claude_code_invoke"
    assert "cleared" in result.content.lower()
    assert (result.data or {}).get("command") == "clear"
    # Session was actually wiped.
    assert session_store.get_session_id("emi_code_room", "main") is None


def test_clear_with_no_active_session_is_idempotent(tmp_path, monkeypatch):
    from app.assistant.lib.tools.claude_code_invoke.claude_code_invoke import ClaudeCodeInvoke
    from app.assistant.utils.pydantic_classes import ToolMessage

    monkeypatch.setattr(session_store, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(coding_agent_runner, "run",
                        lambda *a, **k: pytest.fail("runner.run should not be invoked on /clear"))

    tool = ClaudeCodeInvoke()
    msg = ToolMessage(
        tool_name="claude_code_invoke",
        tool_data={"tool_name": "claude_code_invoke", "arguments": {"task": "/reset"}},
        request_id="test",
        metadata={"room_id": "emi_code_room"},
    )
    result = tool.execute(msg)
    assert result.result_type == "claude_code_invoke"
    assert "no active session" in result.content.lower()
