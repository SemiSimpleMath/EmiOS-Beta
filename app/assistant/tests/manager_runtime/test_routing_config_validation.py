"""_validate_strict_routing_config — the boot-time routing gate.

2026-07-08 strengthening: every state_map VALUE must name a configured agent,
control node, or role binding (a bad value used to surface only as a runtime
"missing agent instance" dead-end mid-request); a ``*_return_control`` KEY
must belong to a configured agent. Keys are otherwise an open vocabulary
(synthetic last_agent signal states like ``<agent>_execute_dag``).

Also sweeps every live manager config through the real validator so a config
regression fails here, not at boot.
"""
from __future__ import annotations

import glob
import os

import pytest
import yaml

from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager


def _validator_shell(config: dict, name: str = "test_manager") -> MultiAgentManager:
    mgr = MultiAgentManager.__new__(MultiAgentManager)
    mgr.name = name
    mgr.manager_config = config
    mgr.flow_config = config.get("flow_config") or {}
    return mgr


def _base_config(state_map: dict) -> dict:
    return {
        "role_bindings": {"delegator": "team::delegator"},
        "agents": [
            {"name": "team::delegator", "class": "Delegator"},
            {"name": "team::planner", "class": "Planner"},
        ],
        "control_nodes": [
            {"name": "tool_caller", "class": "ToolCaller"},
        ],
        "flow_config": {"state_map": state_map},
    }


class TestStateMapValueValidation:

    def test_valid_targets_pass(self):
        cfg = _base_config({
            "team::delegator": "team::planner",
            "team::planner": "tool_caller",
            "tool_caller": "delegator",  # role alias is routable
            "team::planner_return_control": "team::planner",
            "graceful_exit": "tool_caller",  # open key vocabulary
        })
        _validator_shell(cfg)._validate_strict_routing_config()

    def test_unroutable_value_raises(self):
        cfg = _base_config({"team::planner": "ghost_agent"})
        with pytest.raises(ValueError, match="does not name a configured"):
            _validator_shell(cfg)._validate_strict_routing_config()

    def test_return_control_key_with_unknown_prefix_raises(self):
        cfg = _base_config({"ghost_return_control": "team::planner"})
        with pytest.raises(ValueError, match="return_control convention"):
            _validator_shell(cfg)._validate_strict_routing_config()


def test_every_live_manager_config_validates():
    paths = sorted(glob.glob(os.path.join("app", "assistant", "multi_agents", "*", "config.yaml")))
    assert paths, "no manager configs found — wrong working directory?"
    for path in paths:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        mgr = _validator_shell(cfg, name=os.path.basename(os.path.dirname(path)))
        mgr._validate_strict_routing_config()
