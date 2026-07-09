"""Broken tool contracts fail boot (tool-layer audit T1, 2026-07-09).

load_tool_contract used to swallow parse/validation errors (WARN +
contract=None) — and a None contract means NO authority floor
(resolve_tool_min_authority's ceiling-gated exemption), so one bad edit
to a contract JSON silently stripped that tool's authority gate,
approval threshold, and planner card. A present-but-broken contract now
raises into load_tools' fail-loud boot failure list; a MISSING contract
file stays tolerated.
"""
from __future__ import annotations

import textwrap

import pytest

from app.assistant.lib.tool_registry.tool_registry import ToolRegistry


def _write_minimal_tool(root, name: str, contract: str | None) -> None:
    d = root / name
    (d / "tool_forms").mkdir(parents=True)
    (d / "prompts").mkdir()
    (d / f"{name}.py").write_text(textwrap.dedent(f"""
        from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
        from app.assistant.utils.pydantic_classes import ToolResult

        class {name}_cls(BaseTool):
            def execute(self, tool_message):
                return ToolResult(result_type="success", content="ok")

        def get_tool_class():
            return {name}_cls
    """), encoding="utf-8")
    (d / "tool_forms" / "tool_forms.py").write_text(textwrap.dedent(f"""
        from pydantic import BaseModel

        class {name}_args(BaseModel):
            x: str = ""

        class {name}_arguments(BaseModel):
            tool_name: str
            arguments: {name}_args
    """), encoding="utf-8")
    (d / "prompts" / f"{name}_description.j2").write_text("A test tool.", encoding="utf-8")
    if contract is not None:
        (d / "tool_contract.json").write_text(contract, encoding="utf-8")


VALID_CONTRACT = '{"name": "%s", "description": "ok", "inputs": [], "metadata": {"min_authority": 50}}'


def test_valid_contract_loads(tmp_path):
    _write_minimal_tool(tmp_path, "good_tool", VALID_CONTRACT % "good_tool")
    reg = ToolRegistry(str(tmp_path))
    reg.load_tools()
    assert reg.get_tool_contract("good_tool")["metadata"]["min_authority"] == 50


def test_missing_contract_file_is_tolerated(tmp_path):
    _write_minimal_tool(tmp_path, "bare_tool", None)
    reg = ToolRegistry(str(tmp_path))
    reg.load_tools()
    assert "bare_tool" in reg.registry
    assert reg.get_tool_contract("bare_tool") is None


def test_malformed_json_contract_fails_boot(tmp_path):
    _write_minimal_tool(tmp_path, "broken_tool", '{"name": "broken_tool", NOT JSON')
    reg = ToolRegistry(str(tmp_path))
    with pytest.raises(RuntimeError, match="broken_tool"):
        reg.load_tools()


def test_out_of_range_min_authority_fails_boot(tmp_path):
    _write_minimal_tool(
        tmp_path, "hot_tool",
        '{"name": "hot_tool", "inputs": [], "metadata": {"min_authority": 150}}',
    )
    reg = ToolRegistry(str(tmp_path))
    with pytest.raises(RuntimeError, match="hot_tool"):
        reg.load_tools()


def test_non_object_contract_fails_boot(tmp_path):
    _write_minimal_tool(tmp_path, "list_tool", '["not", "an", "object"]')
    reg = ToolRegistry(str(tmp_path))
    with pytest.raises(RuntimeError, match="list_tool"):
        reg.load_tools()
