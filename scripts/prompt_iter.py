"""Reusable prompt-iteration harness for Emi agents.

Workflow:
    1. Define one or more `Scenario`s — each is (agent_input, required
       fragments, optional bounds).
    2. Call `iterate(agent_name, scenarios, runs=N)`.
    3. Read pass/fail report. Edit the agent's prompts/system.j2 or
       user.j2. Re-run. Loop until clean at the stability count you want
       (default runs=1 during iteration; bump to 3 before committing).
    4. Agent prompts/configs reload fresh on each call — no Emi restart
       needed; you don't even need Emi running.

Two invocation modes:

  iterate(...)        — Goes through DI / agent_factory / framework. Use
                        this whenever you can. Works for any provider
                        (OpenAI / Gemini / etc) because it uses the same
                        LLM client Emi uses. Does NOT work for agents
                        whose user_context_items include `recent_history`,
                        `latest_exchange`, or `prior_history` because
                        those require a live scope with messages on the
                        blackboard.

  iterate_jinja(...)  — Renders system.j2 + user.j2 manually with Jinja2,
                        then calls OpenAI directly with the agent's
                        structured-output Pydantic class. Use this when
                        the agent reads `recent_history` and you want to
                        supply your own. OpenAI-only — Gemini path NYI.

Worked examples:
    scratch/final_answer_iter.py          (Jinja mode — needs recent_history)
    scratch/response_formatter_iter.py    (DI mode — no recent_history)
"""
from __future__ import annotations

import dataclasses
import importlib
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Ensure UTF-8 output works on Windows for arrow / em-dash content the
# LLM frequently emits.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Make sibling project imports work when this file is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# DI bootstrap; safe to import multiple times.
import app.assistant.tests.test_setup  # noqa: F401,E402
from app.assistant.ServiceLocator.service_locator import DI  # noqa: E402
from app.assistant.utils.pydantic_classes import (  # noqa: E402
    Message,
    ScopeContext,
    ScopeResourcePolicy,
)


@dataclass
class Scenario:
    """One test case the agent must pass.

    Fields:
        label: short identifier shown in the report (e.g. "karpathy-rich").
        agent_input: dict passed as Message.agent_input. Keys must match
            the agent's user_context_items (task, information, ...) plus
            anything else the prompts reference.
        required_fragments: substrings that MUST appear in the answer.
            Pod IDs, timestamps, URLs, quoted lines, etc.
        forbidden_fragments: substrings the answer must NOT contain.
            E.g. anti-patterns like "see below" or hallucinated bullets.
        max_chars: fail if answer exceeds this length. Use for "brief"
            scenarios — confirms the prompt didn't over-inflate a short
            upstream result.
        min_chars: fail if answer is shorter than this. Use for "rich"
            scenarios when you want a density floor independent of
            fragment checks.
        answer_field: which key on the parsed output holds the text we
            check. Default `final_answer_answer` matches the common
            AgentForm shape. Override per agent if different.
        custom_check: optional callback `(answer, parsed_dict) -> (ok, reason)`
            for assertions that don't fit the four bound checks above.
    """
    label: str
    agent_input: dict[str, Any]
    required_fragments: list[str] = field(default_factory=list)
    forbidden_fragments: list[str] = field(default_factory=list)
    max_chars: int | None = None
    min_chars: int | None = None
    answer_field: str = "final_answer_answer"
    custom_check: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None


def _default_scope(agent_name: str) -> ScopeContext:
    return ScopeContext(
        scope_id=f"harness::{agent_name}",
        owner_id="system",
        actor_id="harness",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


def _extract_answer(result: Any, field_name: str) -> tuple[str, dict[str, Any]]:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        data = result if isinstance(result, dict) else {}
    answer = ""
    if isinstance(data, dict):
        answer = (data.get(field_name) or "")
    if not answer and hasattr(result, "content"):
        answer = getattr(result, "content", "") or ""
    return str(answer), data if isinstance(data, dict) else {}


def _check(scenario: Scenario, answer: str, parsed: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    missing = [f for f in scenario.required_fragments if f not in answer]
    if missing:
        failures.append(f"missing {len(missing)} required fragments: {missing}")
    hits = [f for f in scenario.forbidden_fragments if f in answer]
    if hits:
        failures.append(f"contains forbidden fragments: {hits}")
    if scenario.max_chars is not None and len(answer) > scenario.max_chars:
        failures.append(f"answer {len(answer)} chars exceeds max_chars={scenario.max_chars}")
    if scenario.min_chars is not None and len(answer) < scenario.min_chars:
        failures.append(f"answer {len(answer)} chars below min_chars={scenario.min_chars}")
    if scenario.custom_check is not None:
        ok, reason = scenario.custom_check(answer, parsed)
        if not ok:
            failures.append(f"custom_check: {reason}")
    return failures


def _run_once_di(
    agent_name: str,
    scenario: Scenario,
    *,
    scope: ScopeContext | None = None,
    print_answer: bool = True,
) -> tuple[bool, str, list[str]]:
    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        raise RuntimeError(f"could not create agent: {agent_name}")
    msg = Message(
        agent_input=scenario.agent_input,
        scope_context=scope or _default_scope(agent_name),
    )
    result = agent.action_handler(msg)
    answer, parsed = _extract_answer(result, scenario.answer_field)
    if print_answer:
        print(f"\n[{scenario.label}] len={len(answer)}")
        print("-" * 60)
        print(answer)
        print("-" * 60)
    return len(_check(scenario, answer, parsed)) == 0, answer, _check(scenario, answer, parsed)


def iterate(
    agent_name: str,
    scenarios: list[Scenario],
    *,
    runs: int = 1,
    scope: ScopeContext | None = None,
    print_answer: bool = True,
) -> bool:
    """Run each scenario `runs` times via the live agent (DI path).

    Returns True iff every run of every scenario passed.

    Cannot test agents whose user_context_items include `recent_history`,
    `latest_exchange`, or `prior_history` — those raise because the scope
    has no message history. Use `iterate_jinja` for those.
    """
    print(f"\n=== prompt_iter: agent={agent_name} runs={runs} scenarios={len(scenarios)} ===")
    overall = True
    summary: list[tuple[str, str, int, list[str]]] = []
    for scenario in scenarios:
        for run_idx in range(1, runs + 1):
            label = scenario.label if runs == 1 else f"{scenario.label}#{run_idx}"
            sc = dataclasses.replace(scenario, label=label)
            passed, answer, failures = _run_once_di(
                agent_name, sc, scope=scope, print_answer=print_answer
            )
            summary.append((label, "PASS" if passed else "FAIL", len(answer), failures))
            if not passed:
                overall = False
    _print_summary(agent_name, summary)
    return overall


# ---------------------------------------------------------------------------
# Jinja-direct mode — for agents that need recent_history.
# ---------------------------------------------------------------------------

def _load_agent_form(agent_name: str) -> type:
    """Import the AgentForm class from app/.../<agent>/agent_form.py."""
    # agent_name uses '::' as a manager-namespace separator;
    # the folder mirrors that with /
    rel = agent_name.replace("::", "/")
    mod_path = f"app.assistant.agents.{rel.replace('/', '.')}.agent_form"
    mod = importlib.import_module(mod_path)
    form = getattr(mod, "AgentForm", None)
    if form is None:
        raise RuntimeError(f"{mod_path} does not export `AgentForm`")
    return form


def _load_prompts(agent_name: str) -> tuple[str, str]:
    """Render system.j2 and user.j2 with no user-context variables — caller
    will supply those via Jinja2 dict in `iterate_jinja`."""
    rel = agent_name.replace("::", "/")
    prompt_dir = _REPO_ROOT / "app" / "assistant" / "agents" / rel / "prompts"
    if not prompt_dir.exists():
        raise RuntimeError(f"no prompts dir at {prompt_dir}")
    return (prompt_dir / "system.j2").read_text(encoding="utf-8"), \
           (prompt_dir / "user.j2").read_text(encoding="utf-8")


def iterate_jinja(
    agent_name: str,
    scenarios: list[Scenario],
    *,
    runs: int = 1,
    model: str = "gpt-5-mini",
    template_vars: dict[str, Any] | None = None,
    system_template_vars: dict[str, Any] | None = None,
    print_answer: bool = True,
) -> bool:
    """Render system.j2 + user.j2 with Jinja2, call OpenAI directly,
    parse via the agent's AgentForm.

    Use when the live agent requires `recent_history` / `latest_exchange` /
    `prior_history` and you want to supply your own. Scenario.agent_input's
    keys are passed as Jinja2 vars to user.j2 (e.g. {task}, {information},
    {recent_history}).

    OpenAI only — extend to Gemini by adding a provider arg if needed.
    """
    import jinja2
    from openai import OpenAI

    print(f"\n=== prompt_iter (jinja): agent={agent_name} runs={runs} model={model} ===")
    form_cls = _load_agent_form(agent_name)
    system_raw, user_raw = _load_prompts(agent_name)
    env = jinja2.Environment(loader=jinja2.BaseLoader())
    sys_tpl = env.from_string(system_raw)
    user_tpl = env.from_string(user_raw)

    client = OpenAI()
    overall = True
    summary: list[tuple[str, str, int, list[str]]] = []

    for scenario in scenarios:
        for run_idx in range(1, runs + 1):
            label = scenario.label if runs == 1 else f"{scenario.label}#{run_idx}"
            # System gets framework builtins (date_time, etc) — pass extras
            # via system_template_vars when an agent needs them.
            sys_vars = {"date_time": "2026-05-20 14:00:00 PDT"}
            sys_vars.update(system_template_vars or {})
            user_vars = {"date_time": sys_vars["date_time"]}
            user_vars.update(template_vars or {})
            user_vars.update(scenario.agent_input)

            system_prompt = sys_tpl.render(**sys_vars)
            user_prompt = user_tpl.render(**user_vars)

            resp = client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=form_cls,
            )
            parsed = resp.output_parsed
            parsed_dict = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
            answer = str(parsed_dict.get(scenario.answer_field, "") or "")

            if print_answer:
                print(f"\n[{label}] len={len(answer)}")
                print("-" * 60)
                print(answer)
                print("-" * 60)

            failures = _check(scenario, answer, parsed_dict)
            summary.append((label, "PASS" if not failures else "FAIL", len(answer), failures))
            if failures:
                overall = False

    _print_summary(agent_name, summary)
    return overall


def _print_summary(agent_name: str, rows: list[tuple[str, str, int, list[str]]]) -> None:
    print(f"\n{'=' * 70}\nSUMMARY: {agent_name}\n{'=' * 70}")
    width = max((len(r[0]) for r in rows), default=10)
    for label, status, chars, failures in rows:
        emoji = "PASS" if status == "PASS" else "FAIL"
        print(f"  [{emoji}] {label:<{width}}  {chars:>5} chars")
        for reason in failures:
            print(f"          - {reason}")
    passed = sum(1 for r in rows if r[1] == "PASS")
    print(f"\n  {passed}/{len(rows)} passed\n")
