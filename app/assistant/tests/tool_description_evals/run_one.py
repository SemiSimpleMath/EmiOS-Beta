"""CLI: run the eval harness against one tool.

Usage:
    .venv/Scripts/python.exe -m app.assistant.tests.tool_description_evals.run_one \\
        --tool http_request

    # Test a candidate trim:
    .venv/Scripts/python.exe -m app.assistant.tests.tool_description_evals.run_one \\
        --tool http_request \\
        --override scratch/http_request_candidate.j2

    # Use a different model (default smart-tier matches emi_team::planner):
    .venv/Scripts/python.exe -m app.assistant.tests.tool_description_evals.run_one \\
        --tool http_request --model gpt-5.1-mini

The harness makes real LLM calls. Cost ~$0.01-0.05 per run for ~10 cases.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.assistant.tests.tool_description_evals.eval_harness import (
    _read_tool_description,
    format_report,
    run_suite,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tool", required=True, help="Tool name under test.")
    p.add_argument(
        "--override",
        default=None,
        help="Path to a candidate description file (e.g. a leaner draft). "
             "If omitted, uses the tool's live prompts/<name>_description.j2.",
    )
    p.add_argument(
        "--model",
        default="gpt-5.1",
        help="LLM engine for the test planner (default: gpt-5.1).",
    )
    args = p.parse_args()

    override_path = Path(args.override) if args.override else None

    # Resolve the description length so the report can show it.
    description_text = _read_tool_description(args.tool, override_path=override_path)
    description_chars = len(description_text)

    results = run_suite(
        tool_name=args.tool,
        description_override=override_path,
        model=args.model,
    )

    print(format_report(args.tool, results, description_chars))

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
