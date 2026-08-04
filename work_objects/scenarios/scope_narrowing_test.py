"""
Deterministic proof (no LLM): the orchestrator's ceiling scope, flowed through the
STANDARD scope_adapter, narrows to EXACTLY each manager's declared tools —
web_manager to its 7 web tools, the work manager to work_* + the managers it may
delegate to. This is "build a scope for the orchestrator and everyone gets exactly
the right tools", using the real machinery (no scope_contract_enforced hacks).

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/scope_narrowing_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401 — bootstrap DI

from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

from work_objects.scenarios._scenario_scope import scenario_scope
from work_objects.work_tools import WORK_TOOL_NAMES

WEB_EXPECTED = {"ask_user", "find_tool", "install_tool", "search_web", "scrape_url",
                "http_request", "mcp::npm/server-google-maps::maps_distance_matrix"}


def narrowed_tools(adapter, manager_name, manager_config):
    msg = Message(scope_context=scenario_scope(), task="do work", information="")
    scoped = adapter.apply(manager_name=manager_name, manager_config=manager_config, message=msg)
    return set(scoped.scope_context.tools.allowed_tools or [])


def main() -> None:
    adapter = ScopeAdapter()

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    print("=== orchestrator ceiling scope ('all') -> narrowed per manager ===")

    # web_manager: the real live config, with its scope_contract declaring 7 tools
    web_cfg = DI.manager_registry.get("web_manager")
    web_tools = narrowed_tools(adapter, "web_manager", web_cfg)
    print(f"  web_manager  -> {sorted(web_tools)}")
    ck("web_manager narrowed to exactly its 7 declared web tools", web_tools == WEB_EXPECTED)

    # work manager: scope_contract = the work_* graph tools + the managers it may
    # delegate to (web_manager as a tool — the standard emi_team pattern)
    work_cfg = {"name": "work_manager",
                "scope_contract": {"tools": {"allowed_tools": list(WORK_TOOL_NAMES) + ["web_manager"]}}}
    work_tools = narrowed_tools(adapter, "work_manager", work_cfg)
    print(f"  work_manager -> work_* x{sum(1 for t in work_tools if t.startswith('work_'))} + {[t for t in work_tools if not t.startswith('work_')]}")
    ck("work manager narrowed to work_* + web_manager (so the planner may call web_manager as a tool)",
       work_tools == set(WORK_TOOL_NAMES) | {"web_manager"})

    # the scope is enforced (scope_adapter set the runtime flag — no hand-hacking)
    msg = Message(scope_context=scenario_scope(), task="x")
    scoped = adapter.apply(manager_name="web_manager", manager_config=web_cfg, message=msg)
    ck("scope_adapter set scope_contract_enforced=True on the runtime data",
       scoped.data.get("scope_contract_enforced") is True)
    ck("the enforced allow-list reached task_allowed_tools (what check_tool_access reads)",
       set(scoped.data.get("task_allowed_tools") or []) == WEB_EXPECTED)

    print("\none orchestrator scope -> each manager gets exactly the tools it declares. holds.")


if __name__ == "__main__":
    main()
