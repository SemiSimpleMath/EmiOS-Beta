"""Regression tests for AgentFlowManager.save_agent_config.

The /agent_flow/save_agent_config route called save_agent_config(config) with one arg while the
method requires (config, agents_base_path) -> TypeError -> always 500. These tests pin the method's
contract (writes config.yaml + prompts under the given base) and the name-containment guard, using a
temp base dir so they never touch the real app/assistant/agents tree.
"""
from __future__ import annotations

import pytest
import yaml

from app.assistant.agent_flow_manager.agent_flow_manager import AgentFlowManager


def test_save_writes_config_and_prompts(tmp_path):
    cfg = {
        "name": "test_ns::test_agent",
        "class_name": "SomeAgent",
        "type": "agent",
        "allowed_tools": ["a", "b"],
        "prompts": {"system": "  sys body  ", "user": "user body"},
    }
    AgentFlowManager().save_agent_config(cfg, str(tmp_path))

    agent_dir = tmp_path / "test_ns" / "test_agent"
    saved = yaml.safe_load((agent_dir / "config.yaml").read_text())
    assert saved["name"] == "test_ns::test_agent"
    assert saved["allowed_tools"] == ["a", "b"]
    assert "prompts" not in saved  # prompts are split out to .j2 files
    assert (agent_dir / "prompts" / "system.j2").read_text() == "sys body\n"
    assert (agent_dir / "prompts" / "user.j2").read_text() == "user body\n"


def test_save_rejects_name_traversal(tmp_path):
    cfg = {"name": "../../evil", "class_name": "X", "type": "agent",
           "prompts": {"system": "", "user": ""}}
    with pytest.raises(ValueError):
        AgentFlowManager().save_agent_config(cfg, str(tmp_path))
    assert not (tmp_path.parent / "evil").exists()
