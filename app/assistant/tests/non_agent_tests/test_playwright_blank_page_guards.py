"""Blank-page guards for the Playwright look pipeline (2026-07-25).

The DoorDash order incident: a fresh browser session opens on about:blank,
web_page_coords found zero marks and told the planner to retry, and the retry
(strict=false) took a different code path that raised a fatal RuntimeError —
aborting the run after two actions. Nothing ever told the planner WHERE the
browser was.

Guards:
- zero marks is a SOFT result on both strict paths (never a RuntimeError);
- the soft result names the current URL; on about:blank it says to navigate;
- the page-state node's identity line renders blank-tab guidance;
- the planner user template renders the identity line.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_playwright_blank_page_guards")

import json

import pytest

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.playwright_page_state_node import PlaywrightPageStateNode
from app.assistant.lib.tools.web_page_coords import web_page_coords as wpc_module
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage


@pytest.fixture()
def tool(monkeypatch):
    monkeypatch.setattr(
        DI.tool_registry, "get_mcp_server_entry", lambda sid: {"id": sid, "policy": {}},
    )
    return wpc_module.WebPageCoords()


def _patch_mcp(monkeypatch, *, marks="[]", href="about:blank", title="", identity_fails=False):
    def fake_mcp_call(*, server_entry, tool_name, arguments):
        fn = str((arguments or {}).get("function") or "")
        if "location.href" in fn:
            if identity_fails:
                raise RuntimeError("identity probe boom")
            return json.dumps({"href": href, "title": title}), False, []
        if "const q =" in fn:
            return marks, False, []
        raise AssertionError(f"unexpected mcp_call: {tool_name} fn={fn[:60]!r}")

    monkeypatch.setattr(wpc_module, "mcp_call", fake_mcp_call)


def _msg(**args):
    return ToolMessage(tool_name="web_page_coords", tool_data={"arguments": args})


def test_zero_marks_default_strict_is_soft_and_names_blank_tab(tool, monkeypatch):
    _patch_mcp(monkeypatch, marks="[]", href="about:blank")
    result = tool.execute(_msg(question="search bar"))
    assert result.data["not_ready"] is True
    assert result.data["marks_count"] == 0
    assert result.data["page_url"] == "about:blank"
    assert "blank tab" in result.content
    assert "web_navigate_snapshot" in result.content


def test_zero_marks_strict_false_is_soft_not_fatal(tool, monkeypatch):
    # THE incident guard: strict=false used to fall through to the marks
    # pipeline and raise "No targets produced from marks pipeline."
    _patch_mcp(monkeypatch, marks="[]", href="about:blank")
    result = tool.execute(_msg(question="search bar", strict=False))
    assert result.data["not_ready"] is True
    assert "blank tab" in result.content


def test_zero_marks_on_real_url_names_the_url(tool, monkeypatch):
    _patch_mcp(monkeypatch, marks="[]", href="https://www.doordash.com/home", title="DoorDash")
    result = tool.execute(_msg(question="search bar"))
    assert result.data["not_ready"] is True
    assert "https://www.doordash.com/home" in result.content
    assert result.data["page_url"] == "https://www.doordash.com/home"


def test_zero_marks_identity_probe_failure_stays_soft_without_blank_claim(tool, monkeypatch):
    _patch_mcp(monkeypatch, marks="[]", identity_fails=True)
    result = tool.execute(_msg(question="search bar"))
    assert result.data["not_ready"] is True
    assert result.data["page_url"] is None
    assert "could not be identified" in result.content
    assert "blank tab" not in result.content


def test_mark_injection_crash_non_strict_is_soft(tool, monkeypatch):
    def fake_mcp_call(*, server_entry, tool_name, arguments):
        fn = str((arguments or {}).get("function") or "")
        if "location.href" in fn:
            return json.dumps({"href": "about:blank", "title": ""}), False, []
        raise RuntimeError("evaluate exploded")

    monkeypatch.setattr(wpc_module, "mcp_call", fake_mcp_call)
    result = tool.execute(_msg(question="search bar", strict=False))
    assert result.data["not_ready"] is True


# ---------------------------------------------------------------------------
# Page-state identity line
# ---------------------------------------------------------------------------


def test_identity_format_blank_tab():
    line = PlaywrightPageStateNode._format_page_identity({"href": "about:blank", "title": ""})
    assert "BLANK TAB" in line
    assert "web_navigate_snapshot" in line


def test_identity_format_real_page():
    line = PlaywrightPageStateNode._format_page_identity(
        {"href": "https://www.doordash.com/home", "title": "DoorDash"}
    )
    assert "https://www.doordash.com/home" in line
    assert "DoorDash" in line


def test_identity_format_probe_failed():
    assert "unknown" in PlaywrightPageStateNode._format_page_identity({})


def test_planner_user_template_renders_identity():
    from jinja2 import Environment

    src = (
        get_repo_root()
        / "app" / "assistant" / "agents" / "playwright" / "planner" / "prompts" / "user.j2"
    ).read_text(encoding="utf-8")
    out = Environment().from_string(src).render(
        date_time="now",
        task="t",
        playwright_page_identity="Current page: about:blank — the browser is on a BLANK TAB",
    )
    assert "BLANK TAB" in out
