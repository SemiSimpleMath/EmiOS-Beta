"""kg_resolution_manager — end-to-end test against the live KG.

Bypasses Flask entirely. Bootstraps DI via test_setup, builds the
brief the route would build, calls manager.request_handler(msg)
directly. Prints the structured report when it returns.

WARNING: this mutates the live KG (closes State nodes, regenerates
entity cards, refreshes wiki pages, resolves findings). Pass a
finding_id that's already been resolved, or comment out the actual
invocation, if you don't want side effects.

Usage:
    .venv/Scripts/python.exe -m app.assistant.tests.manager_tests.kg_resolution.kg_resolution_test
"""
from __future__ import annotations

import sys
import time

import app.assistant.tests.test_setup  # noqa  — bootstraps DI
from app.assistant.kg_resolution.resolve_with_prose import _build_brief
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeWritePolicy,
)


def run_resolution_test(*, lead_finding_id: str, prose: str) -> object:
    print(f"\n{'='*70}")
    print(f"Manager test: kg_resolution_manager")
    print(f"Lead finding: {lead_finding_id}")
    print(f"Prose: {prose!r}")
    print(f"{'='*70}\n")

    # Use the same brief-builder the route uses, so behavior is identical.
    brief = _build_brief(lead_finding_id=lead_finding_id, prose=prose)
    if brief is None:
        print(f"ERROR: lead finding {lead_finding_id} not found")
        return None
    print(f"Task: {brief['task']}")
    print(f"\nInformation:\n{brief['information']}\n")

    # Bootstrap manager registry + factory.
    preload_start = time.time()
    DI.manager_registry.preload_all()
    print(f"[preload] {time.time() - preload_start:.2f}s\n")

    manager = DI.multi_agent_manager_factory.create_manager("kg_resolution_manager")
    if manager is None:
        print("ERROR: kg_resolution_manager not registered")
        return None

    msg = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=brief["task"],
        information=brief["information"],
        scope_context=ScopeContext(
            scope_id=f"scope::test::kg_resolution::{int(time.time())}",
            owner_id="jukka",
            actor_id="kg_resolution_manager_test",
            surface="test",
            approval=ScopeApprovalPolicy(authority_level=100),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
            writes=ScopeWritePolicy(write_kg=True, write_unified_log=True),
        ),
    )

    started = time.time()
    print("Invoking manager.request_handler — this can take 30-90s...\n")
    result = manager.request_handler(msg)
    elapsed = time.time() - started

    print(f"\n[run] {elapsed:.2f}s wall")
    print("\n----- RESULT -----")
    print(result)
    print("------------------\n")

    # Pull the structured report off the manager blackboard if available.
    try:
        msgs = manager.blackboard.get_messages()
    except Exception:
        msgs = []
    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        if sender.endswith("final_answer") and "kg_resolution" in sender:
            data = getattr(m, "data", None) or {}
            print("\n----- STRUCTURED REPORT -----")
            for key in ("summary", "completed", "mutations", "regenerations",
                        "findings_resolved", "open_questions"):
                if key in data:
                    print(f"{key}: {data[key]}")
            print("------------------------------\n")
            break

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: kg_resolution_test.py <lead_finding_id> <prose>")
        sys.exit(1)
    run_resolution_test(lead_finding_id=sys.argv[1], prose=sys.argv[2])
