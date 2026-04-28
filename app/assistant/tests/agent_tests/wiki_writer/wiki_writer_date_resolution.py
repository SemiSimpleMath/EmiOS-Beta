"""Test wiki_writer against the 'a year before 2025' compound-error bug.

The bug: writer was given a bullet whose
  - start_date was resolved to 2025-04-22 (estimated)
  - original_sentence preserved 'Around this time last year'
  - utterance occurred 2026-04-22
and produced 'Around a year before 2025, Dave learned ...' (i.e. 2024) by
treating the resolved date as the utterance time and re-applying 'last year'.

The fix has two parts:
  1. ``wiki_renderer`` now appends a ``[utterance: <date>; date_confidence: <c>]``
     metadata line under each State/Event bullet so the writer sees the full
     temporal frame.
  2. ``wiki_writer/prompts/system.j2`` has a "Reading source dates" section
     telling the agent how to interpret ``since`` vs ``utterance`` vs the
     blockquote, and explicitly forbidding the source-relative re-injection.

This test feeds the writer a bullet in the new format and verifies the prose:
  - does NOT contain compound phrases like 'a year before 2025'
  - does NOT mention 2024
  - DOES anchor on 2025

LLM output is non-deterministic; we run N trials and report pass rate. Two
out of three trials passing is the bar — the prompt rule should win the
clear majority of the time once the writer can see the metadata.

Run via:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.wiki_writer.wiki_writer_date_resolution
"""
from __future__ import annotations

import re
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)


_ROUGH_PAGE = """\
## Personal life

- **has state** [[Dave]] since 2025-04-22 — *Pregnancy discovery* {node:11111111-2222-3333-4444-555555555555}
  > Around this time last year, Dave found out that his wife was pregnant with someone else's baby.
  [utterance: 2026-04-22; date_confidence: estimated]
"""


_BAD_PATTERNS = [
    r"year before 2025",
    r"year prior to 2025",
    r"year earlier than 2025",
    r"year preceding 2025",
    r"\b2024\b",  # the wrong year — mention of 2024 in any form
]


_N_TRIALS = 3


def _scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::test::wiki_writer_date_resolution",
        owner_id="jukka",
        actor_id="test_runner",
        surface="ui",
        room_id="master_room",
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


def _run_one_trial(trial_index: int) -> dict:
    agent = DI.agent_factory.create_agent("wiki_writer")
    if agent is None:
        raise RuntimeError("agent_factory returned None for 'wiki_writer'")

    msg = Message(
        agent_input={
            "entity_name": "Dave",
            "rough_page": _ROUGH_PAGE,
            "window_excerpts": "",
            "section_slug": "personal_life",
            "themes_already_covered": "",
        },
        scope_context=_scope(),
    )
    result = agent.action_handler(msg)
    data = getattr(result, "data", None) or {}
    page_md = data.get("page_markdown") or ""

    bad_hits = [p for p in _BAD_PATTERNS if re.search(p, page_md, flags=re.IGNORECASE)]
    has_2025 = bool(re.search(r"\b2025\b", page_md))
    passed = (not bad_hits) and has_2025

    return {
        "trial": trial_index,
        "page_markdown": page_md,
        "bad_hits": bad_hits,
        "has_2025": has_2025,
        "passed": passed,
    }


def main() -> int:
    print(f"=== wiki_writer date-resolution test ({_N_TRIALS} trials) ===\n")
    results = []
    for i in range(1, _N_TRIALS + 1):
        print(f"--- Trial {i} ---")
        try:
            r = _run_one_trial(i)
            results.append(r)
            print(r["page_markdown"])
            verdict = "PASS" if r["passed"] else "FAIL"
            print(f"\n[{verdict}] bad_hits={r['bad_hits']} has_2025={r['has_2025']}")
        except Exception as e:
            print(f"CRASH: {type(e).__name__}: {e}")
            results.append({"trial": i, "passed": False, "error": str(e)})
        print()

    n_pass = sum(1 for r in results if r.get("passed"))
    print(f"=== {n_pass}/{_N_TRIALS} trials passed ===")
    # Bar: majority. Single-trial flakiness on a permissive LLM is acceptable
    # if the rule is winning most of the time.
    return 0 if n_pass >= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
