"""An agent offered as a delegate must be able to do work, and must say what it does.

2026-09-11: `personal_admin::planner` carried `allowed_nodes: [personal_admin::final_answer]`
— its own manager's EXIT agent, which holds no tools and whose only job is to close the task
out. The injector renders allowed_nodes as an "Agents:" list, and the planner prompt says an
agent call takes "a precise task description for the sub-agent", so the planner handed it ten
calendar writes. That agent has no description file either, so it rendered as a bare name with
a blank after it; across twenty turns the planner invented an identity for it, escalating from
"the final_answer agent" to "the execution agent that manages calendar and scheduler
operations". Nothing was created for twenty rounds.

Two rules, checked over every agent config in the tree:

  1. No agent may list a terminal `*::final_answer` in allowed_nodes. Exiting is what
     `return_control` is for — the manager's state_map already routes a planner's
     return_control to its final_answer.
  2. Every agent that IS listed as a delegate must have a non-empty description, because the
     Agents block renders `prompts/description.j2` and a blank reads as "general purpose".

Hermetic — reads config files only.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.utils.path_utils import get_repo_root

AGENTS_DIR = get_repo_root() / "app" / "assistant" / "agents"
# Intrinsic actions the runtime resolves itself — never agent names.
_INTRINSIC = {"return_control", "done", "step_complete", "manager_exit_node", "all"}


def _agent_configs() -> list[tuple[str, dict, Path]]:
    out = []
    for cfg_path in AGENTS_DIR.rglob("config.yaml"):
        if "_archived" in cfg_path.parts:
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(cfg, dict) or not cfg.get("name"):
            continue
        out.append((str(cfg["name"]).strip(), cfg, cfg_path))
    return out


def _declared_nodes(cfg: dict) -> list[str]:
    raw = cfg.get("allowed_nodes")
    if not isinstance(raw, list):
        return []
    return [str(n).strip() for n in raw if str(n).strip() not in _INTRINSIC]


def test_configs_were_found():
    assert len(_agent_configs()) > 50, "agent configs not discovered — check AGENTS_DIR"


def test_no_agent_delegates_to_a_terminal_final_answer():
    offenders = [
        f"{name} -> {node}  ({path.relative_to(get_repo_root())})"
        for name, cfg, path in _agent_configs()
        for node in _declared_nodes(cfg)
        if node.endswith("::final_answer") or node == "final_answer"
    ]
    assert not offenders, (
        "These agents list an exit agent as a delegable node. Exiting is return_control's job; "
        "a final_answer has no tools and cannot perform the work:\n  " + "\n  ".join(offenders)
    )


def test_every_delegable_agent_describes_itself():
    by_name = {name: path.parent for name, _cfg, path in _agent_configs()}
    missing = []
    for name, cfg, path in _agent_configs():
        for node in _declared_nodes(cfg):
            target_dir = by_name.get(node)
            if target_dir is None:
                continue  # unresolvable target is a different test's problem
            desc = target_dir / "prompts" / "description.j2"
            if not desc.exists() or not desc.read_text(encoding="utf-8").strip():
                missing.append(f"{name} -> {node} (no non-empty {desc.relative_to(get_repo_root())})")
    assert not missing, (
        "A delegable agent renders into the planner's 'Agents:' list; with no description it "
        "renders as a bare name and the caller invents a capability for it:\n  " + "\n  ".join(missing)
    )
