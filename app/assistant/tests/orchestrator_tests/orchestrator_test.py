"""
Free-form orchestrator runner (ad-hoc, not a unit test).

Edit only the DEFAULT_TASK string and run it (IDE-friendly).

Usage:
  python app/assistant/tests/orchestrator_tests/orchestrator_test.py --task "Atomic number of gold and boiling point of water, then combine them"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

#IDE-friendly default (edit this line).
DEFAULT_TASK = """Val Kilmer is coming to my house for dinner.
Check his wikipedia to see what he would like to see and what day he would be available.
 Also research in some food magazines what his favorite dishes.
 search the web for preference for the day of week for dinner.  Then email my partner to let them know of the day he is coming."""


# DEFAULT_TASK = """Find out Leonardo Di Caprio's current gf age.  Email the results to user@example.com """

# Ensure repo root on sys.path (mirrors other tests).
_HERE = Path(__file__).resolve()
for parent in _HERE.parents:
    if (parent / "app").is_dir():
        sys.path.insert(0, str(parent))
        break

import app.assistant.tests.test_setup  # noqa: F401 (side-effect: initializes DI)
from app.assistant.ServiceLocator.service_locator import DI

def main():
    # For ad-hoc runs in the IDE: print the architect prompt at first run,
    # and print a simple orchestrator step trace.
    os.environ.setdefault("EMI_PRINT_ARCHITECT_PROMPT", "1")
    os.environ.setdefault("EMI_PRINT_ORCH_SEQUENCE", "1")
    # Print system + user prompts for all LLM calls (noisy but useful).
    os.environ.setdefault("EMI_PRINT_PROMPTS", "1")

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--task",
        default=DEFAULT_TASK,
        help="Single objective string to feed the orchestrator (optional; defaults to DEFAULT_TASK in file).",
    )
    args = ap.parse_args()

    orch = DI.orchestrator_factory.create_orchestrator("orchestrator_test")
    t0 = time.time()
    res = orch.run(str(args.task), depth=0)
    elapsed = time.time() - t0

    print("\n=== Final Orchestrator Result ===")
    print(res)
    print(f"Elapsed seconds: {elapsed:.2f}")

    # Show the last architect decision that was written to the blackboard.
    spawn = orch.blackboard.get_state_value("spawn", [])
    notes = orch.blackboard.get_state_value("notes", "")
    print("\n=== Last Architect Output (blackboard.spawn) ===")
    print(spawn)
    print("\nArchitect notes:")
    print(notes)

    # Optional: show the exact architect prompt captured on first run.
    prompt_msgs = orch.blackboard.get_state_value("orchestrator_architect_prompt_messages", None)
    if prompt_msgs:
        print("\n=== Architect Prompt (captured on first run) ===")
        try:
            for m in prompt_msgs:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content")
                print(f"\n[{role}]\n{content}\n")
        except Exception:
            print(prompt_msgs)

    results = orch.blackboard.get_state_value("orchestrator_results", [])
    print("\n=== Recent Results (orchestrator_results) ===")
    print(results)


if __name__ == "__main__":
    main()

