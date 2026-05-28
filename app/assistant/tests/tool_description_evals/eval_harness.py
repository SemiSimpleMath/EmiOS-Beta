"""Test harness for measuring whether a tool's description is sufficient.

One LLM call per test case. The planner-style prompt gets a task + a
small tool catalog (the tool under test + a few realistic distractors).
The LLM must pick the right tool AND produce a plausible action_input.
Per-case assertions check routing (`expect_action`) and calling
(`expect_args_contain` / `expect_args_match`).

Designed for iterative leanness: trim the tool's description, re-run the
suite, see what breaks. Description quality = (passes / total cases).

This is NOT a unit test in the pytest sense — it makes real LLM calls
and measures probabilistic behavior. Run it as a CLI tool when iterating
on tool descriptions, not in CI.

Layout:
    app/assistant/tests/tool_description_evals/
        eval_harness.py       (this file)
        cases/<tool>.yaml     per-tool test cases
        run_one.py            CLI: --tool <name> [--override <path-to-.j2>]
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel

# Bootstrap (path setup) — but we bypass DI for the LLM call so the
# harness can run as a standalone CLI without the full server stack.
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.utils.path_utils import get_repo_root
from app.services.llm_client import LLMInterface, OpenAILLM


TOOLS_ROOT = get_repo_root() / "app" / "assistant" / "lib" / "tools"
CASES_DIR = Path(__file__).parent / "cases"


# ---------------------------------------------------------------------------
# Output schema the test planner emits
# ---------------------------------------------------------------------------


class TestPlannerOutput(BaseModel):
    """Schema for the test planner LLM call.

    `action_input` is a JSON-encoded string (not a dict) because OpenAI's
    structured-output strict mode rejects open-ended dicts — `Dict[str, Any]`
    can't declare `additionalProperties: false`. The harness json.loads it
    after the call; check_case sees a real dict.
    See memory: project_openai_strict_mode_dict_any.md.
    """
    reasoning: str
    action: str
    action_input: str = "{}"


# ---------------------------------------------------------------------------
# Tool description loading
# ---------------------------------------------------------------------------


def _read_tool_description(tool_name: str, override_path: Optional[Path] = None) -> str:
    """Read a tool's description from its prompts/<name>_description.j2,
    or from `override_path` when testing a candidate trim.

    Falls back to `tool_contract.json::description` if no .j2 file
    exists (some tools don't have one; the contract description is the
    canonical short form).
    """
    if override_path is not None:
        return override_path.read_text(encoding="utf-8").strip()

    j2 = TOOLS_ROOT / tool_name / "prompts" / f"{tool_name}_description.j2"
    if j2.exists():
        return j2.read_text(encoding="utf-8").strip()

    contract = TOOLS_ROOT / tool_name / "tool_contract.json"
    if contract.exists():
        c = json.loads(contract.read_text(encoding="utf-8"))
        return str(c.get("description") or "").strip()

    raise FileNotFoundError(f"No description found for tool {tool_name!r}")


# ---------------------------------------------------------------------------
# Catalog rendering
# ---------------------------------------------------------------------------


def build_catalog(
    *,
    tool_under_test: str,
    distractors: List[str],
    description_override: Optional[Path] = None,
) -> str:
    """Render the tool list the test planner sees.

    The tool under test gets its description from `description_override`
    when provided (so the harness can A/B candidate trims against the
    live version). Distractors always use their live descriptions.
    """
    lines: List[str] = ["Available tools:"]
    for name in [tool_under_test, *distractors]:
        override = description_override if name == tool_under_test else None
        desc = _read_tool_description(name, override_path=override)
        # Single-line collapse for the catalog header; multi-line descriptions
        # are kept verbatim under the header so the planner can still parse
        # blocks of detail when present.
        lines.append(f"\n- {name}:")
        lines.append(desc)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM invocation
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You are a planner agent. Given a task and a list of available tools, "
    "choose the single best tool from the list and produce a plausible "
    "action_input. Be concise. Do not invent tool names — `action` MUST "
    "be one of the names in the tool list. If the user's task is ambiguous, "
    "pick the tool that best matches the literal request; do not assume "
    "additional steps that aren't asked for."
)


_LLM_INTERFACE: Optional[LLMInterface] = None


def _llm_interface() -> LLMInterface:
    global _LLM_INTERFACE
    if _LLM_INTERFACE is None:
        _LLM_INTERFACE = LLMInterface(OpenAILLM())
    return _LLM_INTERFACE


def run_test_case(
    *,
    task: str,
    catalog: str,
    model: str = "gpt-5.1",
) -> TestPlannerOutput:
    """One LLM call. Returns the planner's chosen action + args."""
    user_prompt = (
        f"{catalog}\n\nTask: {task}\n\n"
        "Respond with JSON. action_input must be a JSON-encoded STRING "
        "(e.g. '{\"url\": \"https://...\", \"method\": \"POST\"}'). Fields:\n"
        "  reasoning: str (one sentence)\n"
        "  action: str (tool name from the list)\n"
        "  action_input: str (JSON-encoded args)\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    raw = _llm_interface().structured_output(
        messages,
        engine=model,
        response_format=TestPlannerOutput,
        temperature=0,
        timeout=120,
    )
    if isinstance(raw, dict):
        return TestPlannerOutput.model_validate(raw)
    if isinstance(raw, TestPlannerOutput):
        return raw
    raise RuntimeError(f"Unexpected planner output type: {type(raw)}")


# ---------------------------------------------------------------------------
# Assertion checking
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    name: str
    task: str
    passed: bool
    failures: List[str] = field(default_factory=list)
    output: Optional[TestPlannerOutput] = None


def _parse_action_input(raw: str) -> Dict[str, Any]:
    """Decode the planner's action_input string into a dict for assertions.

    The planner emits action_input as a JSON-encoded string (see
    TestPlannerOutput). Empty / unparseable inputs become {} so the
    harness can still run assertions cleanly.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def check_case(case: Dict[str, Any], output: TestPlannerOutput) -> List[str]:
    """Returns a list of assertion failure messages; empty list = passed."""
    failures: List[str] = []
    args = _parse_action_input(output.action_input)
    expect_action = case.get("expect_action")
    if expect_action is not None and output.action != expect_action:
        failures.append(
            f"action mismatch: expected {expect_action!r}, got {output.action!r}"
        )
    expect_not = case.get("expect_action_not")
    if expect_not is not None:
        not_set = [expect_not] if isinstance(expect_not, str) else list(expect_not)
        if output.action in not_set:
            failures.append(f"action {output.action!r} is in expect_action_not={not_set}")
    expect_contain = case.get("expect_args_contain")
    if isinstance(expect_contain, list):
        missing = [k for k in expect_contain if k not in args]
        if missing:
            failures.append(f"missing required args keys: {missing}")
    expect_match = case.get("expect_args_match")
    if isinstance(expect_match, dict):
        for k, v in expect_match.items():
            actual = args.get(k)
            if actual != v:
                failures.append(f"args[{k!r}] mismatch: expected {v!r}, got {actual!r}")
    return failures


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------


def load_cases(tool_name: str) -> Dict[str, Any]:
    p = CASES_DIR / f"{tool_name}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"No cases file: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def run_suite(
    *,
    tool_name: str,
    description_override: Optional[Path] = None,
    model: str = "gpt-5.1",
) -> List[CaseResult]:
    spec = load_cases(tool_name)
    distractors = list(spec.get("distractors") or [])
    cases = list(spec.get("cases") or [])
    catalog = build_catalog(
        tool_under_test=tool_name,
        distractors=distractors,
        description_override=description_override,
    )

    results: List[CaseResult] = []
    for case in cases:
        name = str(case.get("name") or "(unnamed)")
        task = str(case.get("task") or "")
        try:
            output = run_test_case(task=task, catalog=catalog, model=model)
            fails = check_case(case, output)
            results.append(CaseResult(
                name=name, task=task,
                passed=not fails, failures=fails, output=output,
            ))
        except Exception as e:
            results.append(CaseResult(
                name=name, task=task,
                passed=False, failures=[f"harness error: {type(e).__name__}: {e}"],
            ))
    return results


def format_report(tool_name: str, results: List[CaseResult], description_chars: int) -> str:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    lines = [
        f"=" * 70,
        f"TOOL: {tool_name}",
        f"description length: {description_chars} chars",
        f"PASSED: {passed}/{total}",
        f"=" * 70,
    ]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"[{status}] {r.name}: {r.task[:80]}")
        if r.output is not None:
            lines.append(f"    → action={r.output.action!r} action_input={r.output.action_input}")
        for f in r.failures:
            lines.append(f"    ✗ {f}")
    return "\n".join(lines)
