"""Delegation accuracy test for master_room::switchboard.

Each case is a synthetic (task, information) pair that the chat_gate
might hand to the switchboard. We compare the agent's `delegate_to`
against the expected manager (plus any accept_also alternates).

Run:
  .venv\\Scripts\\python.exe app/assistant/tests/agent_tests/switchboard/delegation_test.py

Costs ~50 nano-tier LLM calls (pennies). Each agent instance is fresh
(new Blackboard) so case order doesn't leak state.
"""
from __future__ import annotations

import os
import sys
import time
from collections import Counter, defaultdict

# Add repo root so we can import as a package even when run as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import app.assistant.tests.test_setup  # noqa: F401 — bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)
from app.assistant.tests.agent_tests.switchboard.delegation_cases import CASES, Case

AGENT_NAME = "master_room::switchboard"


def _scope() -> ScopeContext:
    """Minimal master-room-like scope so resource_assistant_data resolves."""
    return ScopeContext(
        scope_id="scope::test::switchboard_delegation",
        owner_id="jukka",
        actor_id="test_runner",
        surface="ui",
        room_id="master_room",
        approval=ScopeApprovalPolicy(authority_level=99),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


def _run_case(case: Case) -> dict:
    agent = DI.agent_factory.create_agent(AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {AGENT_NAME!r}")
    agent.blackboard.update_state_value("task", case.task)
    agent.blackboard.update_state_value("information", case.information)

    t0 = time.time()
    try:
        result = agent.action_handler(Message(scope_context=_scope()))
    except Exception as e:
        return {
            "id": case.id,
            "expected": case.expected,
            "accept_also": list(case.accept_also or ()),
            "actual": f"ERROR: {type(e).__name__}: {e}",
            "passed": False,
            "task": case.task,
            "reason": "",
            "elapsed_s": time.time() - t0,
        }
    elapsed = time.time() - t0

    data = getattr(result, "data", None) or {}
    actual = str(data.get("delegate_to") or "").strip() or "(none)"
    accept = {case.expected, *(case.accept_also or ())}
    passed = actual in accept

    return {
        "id": case.id,
        "expected": case.expected,
        "accept_also": list(case.accept_also or ()),
        "actual": actual,
        "passed": passed,
        "task": case.task,
        "reason": str(data.get("reason") or "")[:240],
        "elapsed_s": elapsed,
    }


def main() -> None:
    print(f"Running {len(CASES)} delegation cases against {AGENT_NAME}...\n")
    per_cat = defaultdict(lambda: {"pass": 0, "total": 0})
    confusion: Counter = Counter()
    results: list[dict] = []

    for i, c in enumerate(CASES, 1):
        r = _run_case(c)
        results.append(r)

        per_cat[c.category]["total"] += 1
        if r["passed"]:
            per_cat[c.category]["pass"] += 1
        else:
            confusion[(c.expected, r["actual"])] += 1

        mark = "PASS" if r["passed"] else "FAIL"
        also = f" (also OK: {', '.join(r['accept_also'])})" if (not r["passed"] and r["accept_also"]) else ""
        print(f"  [{i:2d}/{len(CASES)}] {mark}  {c.id:32s}  expected={c.expected:24s}  actual={r['actual']:24s}{also}  ({r['elapsed_s']:.1f}s)")
        if not r["passed"]:
            print(f"           task   : {c.task}")
            print(f"           reason : {r['reason']}")
            if c.notes:
                print(f"           notes  : {c.notes}")

    # -----------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------
    print()
    print("=" * 78)
    print("PER-CATEGORY BREAKDOWN")
    print("=" * 78)
    for cat in sorted(per_cat):
        s = per_cat[cat]
        pct = (s["pass"] / s["total"]) * 100 if s["total"] else 0.0
        print(f"  {cat:18s}  {s['pass']:>3d} / {s['total']:>3d}   {pct:5.1f}%")

    passed_total = sum(1 for r in results if r["passed"])
    pct_total = passed_total / len(results) * 100 if results else 0.0
    total_time = sum(r["elapsed_s"] for r in results)
    print()
    print("=" * 78)
    print(f"OVERALL: {passed_total} / {len(results)}  ({pct_total:.1f}%)   total time: {total_time:.1f}s")
    print("=" * 78)

    if confusion:
        print()
        print("CONFUSION TABLE (expected -> actual, count):")
        for (exp, act), n in confusion.most_common():
            print(f"  {n:>2d} x  {exp:24s} -> {act}")


if __name__ == "__main__":
    main()
