"""Smoke test for knowledge_graph_add::fact_canonicalizer.

Feeds the agent ~12 source sentences across the major shapes (past/present/
future tense, durations, age claims, named events, goals, and a few not in
the prompt's example list) and asserts the canonical output:

- Is non-empty.
- Contains no temporal phrases ("ago", "yesterday", "in 2025", "for 4
  months", "back in", etc.).
- Contains no first- or second-person pronouns when participants were
  provided (the agent should de-pronoun against the participant list).

Each case runs once. Per-case pass/fail printed; final tally returned as
exit code (0 = all passed, 1 = any failed).

Run via:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.fact_canonicalizer.fact_canonicalizer
"""
from __future__ import annotations

import re
import sys
from typing import Optional

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)


_AGENT_NAME = "knowledge_graph_add::fact_canonicalizer"


# Patterns that should NEVER appear in the canonical output.
_TEMPORAL_PATTERNS = [
    (r"\bago\b", "'ago'"),
    (r"\byesterday\b", "'yesterday'"),
    (r"\btomorrow\b", "'tomorrow'"),
    (r"\bnext (month|year|week|day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "'next <unit>'"),
    (r"\blast (month|year|week|day|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "'last <unit>'"),
    (r"\bthis (month|year|week|morning|afternoon|evening)\b", "'this <unit>'"),
    (r"\btonight\b", "'tonight'"),
    (r"\b(in|by|since|until|during|from)\s+\d{4}\b", "year reference (in/by/since/... <year>)"),
    (r"\bfor \d+ (year|month|day|week|hour|minute)s?\b", "duration ('for N <unit>')"),
    (r"\bin \d+ (year|month|day|week)s?\b", "future offset ('in N <unit>')"),
    (r"\baround the time\b", "'around the time'"),
    (r"\bback in\b", "'back in'"),
    (r"\brecently\b", "'recently'"),
    (r"\b(soon|earlier today|later today)\b", "'soon/earlier/later'"),
    (r"\bcurrently\b", "'currently'"),
]


# First/second-person pronouns that should be substituted with named participants.
_PRONOUN_PATTERNS = [
    (r"\b(I|I've|I'm|I'd|I'll|I’m|I’ve|I’d|I’ll)\b", "first-person 'I'"),
    (r"\b(my|me|mine|myself)\b", "first-person 'my/me'"),
    (r"\b(we|us|our|ours|ourselves)\b", "first-person plural"),
    (r"\b(you|your|yours|yourself)\b", "second-person"),
]


def _scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::test::fact_canonicalizer",
        owner_id="jukka",
        actor_id="test_runner",
        surface="ui",
        room_id="master_room",
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


def _check_violations(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for pat, label in patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            out.append(label)
    return out


def _run_case(
    *,
    name: str,
    extractor_sentence: str,
    node_label: str,
    node_type: str,
    participants: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    valid_during: Optional[str] = None,
) -> dict:
    agent = DI.agent_factory.create_agent(_AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {_AGENT_NAME!r}")

    agent_input: dict = {
        "extractor_sentence": extractor_sentence,
        "node_label": node_label,
        "node_type": node_type,
    }
    if participants:
        agent_input["participants"] = participants
    if start_date:
        agent_input["start_date"] = start_date
    if end_date:
        agent_input["end_date"] = end_date
    if valid_during:
        agent_input["valid_during"] = valid_during

    result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    data = getattr(result, "data", None) or {}
    canonical = data.get("canonical_sentence") or ""

    temporal_hits = _check_violations(canonical, _TEMPORAL_PATTERNS)
    pronoun_hits: list[str] = []
    if participants:  # only enforce de-pronouning when participants were given
        pronoun_hits = _check_violations(canonical, _PRONOUN_PATTERNS)

    is_empty = not canonical.strip()
    passed = (not is_empty) and (not temporal_hits) and (not pronoun_hits)

    return {
        "name": name,
        "input": extractor_sentence,
        "canonical": canonical,
        "temporal_hits": temporal_hits,
        "pronoun_hits": pronoun_hits,
        "is_empty": is_empty,
        "passed": passed,
    }


# Test cases. Mix of "in the prompt's example list" (sanity) and "novel" (generalization).
_CASES = [
    # In-prompt — sanity that the agent at least reproduces its examples.
    dict(
        name="dave_affair_event",
        extractor_sentence="About a year ago Dave's wife cheated on him.",
        node_label="Affair",
        node_type="Event",
        participants="Dave, Dave's wife",
        start_date="2025-04-22",
        valid_during="one-off",
    ),
    dict(
        name="bonnie_age_state",
        extractor_sentence="Bonnie is about 3-4 years old.",
        node_label="Age",
        node_type="State",
        participants="Bonnie",
        valid_during="as of 2024-12-30",
    ),
    dict(
        name="future_age_state",
        extractor_sentence="In 3 years I will be 50.",
        node_label="Age",
        node_type="State",
        participants="Jukka",
        start_date="2029-04-22",
    ),
    dict(
        name="diet_duration_state",
        extractor_sentence="I have been dieting for 4 months.",
        node_label="Diet",
        node_type="State",
        participants="Jukka",
        start_date="2025-12-22",
    ),
    # Novel — not literally in the prompt's example list.
    dict(
        name="laptop_purchase_event",
        extractor_sentence="Yesterday I bought a new MacBook Pro.",
        node_label="Laptop purchase",
        node_type="Event",
        participants="Jukka",
        start_date="2026-04-21",
    ),
    dict(
        name="tokyo_future_residence",
        extractor_sentence="Next month I'll be in Tokyo.",
        node_label="Tokyo trip",
        node_type="State",
        participants="Jukka, Tokyo",
        start_date="2026-05-22",
    ),
    dict(
        name="nephew_age_named",
        extractor_sentence="My nephew Ben is 7 years old.",
        node_label="Age",
        node_type="State",
        participants="Ben",
        valid_during="as of 2026-04-22",
    ),
    dict(
        name="google_long_employment",
        extractor_sentence="Katy has been working at Google for 6 years.",
        node_label="Employment",
        node_type="State",
        participants="Katy, Google",
        start_date="2020-04-22",
    ),
    dict(
        name="marathon_goal_year",
        extractor_sentence="This year I want to run a marathon.",
        node_label="Run a marathon",
        node_type="Goal",
        participants="Jukka",
    ),
    dict(
        # Tests the "places aren't subjects" rule on a case NOT in the prompt's
        # example list (Chez Panisse instead of Napa).
        name="dinner_out_event",
        extractor_sentence="We had dinner at Chez Panisse last Friday for our anniversary.",
        node_label="Anniversary dinner",
        node_type="Event",
        participants="Jukka, Katy, Chez Panisse",
        start_date="2026-04-17",
    ),
    dict(
        name="recovery_state_past_tense",
        extractor_sentence="I was recovering from knee surgery for two months.",
        node_label="Knee recovery",
        node_type="State",
        participants="Jukka",
        start_date="2026-02-22",
        end_date="2026-04-22",
    ),
    dict(
        name="meeting_event_with_year",
        extractor_sentence="We met at a coffee shop in Berkeley back in 2003.",
        node_label="First meeting",
        node_type="Event",
        participants="Jukka, Katy, Berkeley",
        start_date="2003-01-01",
    ),
    dict(
        # Belief State held now whose content is future-tense. The Belief
        # itself is current; the content references a future state. Tests
        # whether the canonicalizer keeps "believes" (present) and handles
        # the future-content reference reasonably.
        name="belief_about_future_state",
        extractor_sentence="I believe in 2 years I will be bald.",
        node_label="Belief",
        node_type="State",
        participants="Jukka",
    ),
]


def main() -> int:
    print(f"=== fact_canonicalizer smoke test ({len(_CASES)} cases) ===\n")
    results = []
    for case in _CASES:
        try:
            r = _run_case(**case)
        except Exception as e:
            r = {"name": case["name"], "passed": False, "error": f"{type(e).__name__}: {e}"}
        results.append(r)

        verdict = "PASS" if r.get("passed") else "FAIL"
        print(f"[{verdict}] {r['name']}")
        if "input" in r:
            print(f"   in:  {r['input']}")
            print(f"   out: {r.get('canonical', '')!r}")
        if r.get("temporal_hits"):
            print(f"   temporal hits: {r['temporal_hits']}")
        if r.get("pronoun_hits"):
            print(f"   pronoun hits:  {r['pronoun_hits']}")
        if r.get("is_empty"):
            print("   (empty output)")
        if "error" in r:
            print(f"   ERROR: {r['error']}")
        print()

    n_pass = sum(1 for r in results if r.get("passed"))
    print(f"=== {n_pass}/{len(_CASES)} cases passed ===")
    return 0 if n_pass == len(_CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
