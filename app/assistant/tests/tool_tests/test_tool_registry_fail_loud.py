"""Tool registry fails loud on an unloadable tool (2026-06-13).

A core tool that can't load (bad import, syntax error, or a malformed
tool_contract — including the min_authority/approval_min_authority range
checks) must NOT silently vanish from the registry while boot reports
success. A missing security-gated tool is a defect to fix, not skip past.
"""
import pytest

from app.assistant.lib.tool_registry.tool_registry import ToolRegistry


def test_load_tools_raises_on_unloadable_tool(tmp_path):
    broken = tmp_path / "broken_tool"
    broken.mkdir()
    # Guaranteed to fail at module load regardless of the loader mechanism.
    (broken / "broken_tool.py").write_text("this is not valid python :::(\n")

    reg = ToolRegistry(tools_dir=tmp_path)
    with pytest.raises(RuntimeError, match="failed to load"):
        reg.load_tools()


def test_load_tools_clean_when_empty(tmp_path):
    # No tools -> no failures -> no raise (the guard only fires on real failures).
    reg = ToolRegistry(tools_dir=tmp_path)
    reg.load_tools()
    assert reg.registry == {}
