"""Integration test: meta_data_add (best-guess time bounds) + fact_canonicalizer
(present-tense rewrite) running back-to-back, the way the proposal pipeline
will use them.

For each test case:
  1. Call ``knowledge_graph_add::meta_data_add`` with the source sentence and
     utterance timestamp to get start_date / end_date / valid_during.
  2. Call ``knowledge_graph_add::fact_canonicalizer`` with the source +
     dates from step 1 to get the present-tense canonical sentence.
  3. Assert:
     - meta_data_add resolved the time bounds correctly (specific expected
       date for relative phrases, empty for point-in-time observations,
       ongoing for unbounded states).
     - canonicalizer output is non-empty, present tense (no temporal phrases),
       and uses canonical entity names instead of pronouns.

The point: prove the two agents produce a coherent (present-tense fact +
correct validity dates) pair. Each case runs once.

Run via:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.fact_canonicalizer.pipeline_integration
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Optional

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)


META_AGENT = "knowledge_graph_add::meta_data_add"
CANONICALIZER_AGENT = "knowledge_graph_add::fact_canonicalizer"


_TEMPORAL_PATTERNS = [
    (r"\bago\b", "'ago'"),
    (r"\byesterday\b", "'yesterday'"),
    (r"\btomorrow\b", "'tomorrow'"),
    (r"\bnext (month|year|week|day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "'next <unit>'"),
    (r"\blast (month|year|week|day|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "'last <unit>'"),
    (r"\bthis (month|year|week|morning|afternoon|evening)\b", "'this <unit>'"),
    (r"\btonight\b", "'tonight'"),
    (r"\b(in|by|since|until|during|from)\s+\d{4}\b", "year reference"),
    (r"\bfor \d+ (year|month|day|week|hour|minute)s?\b", "duration ('for N <unit>')"),
    (r"\bin \d+ (year|month|day|week)s?\b", "future offset ('in N <unit>')"),
    (r"\baround the time\b", "'around the time'"),
    (r"\bback in\b", "'back in'"),
    (r"\brecently\b", "'recently'"),
    (r"\bcurrently\b", "'currently'"),
]


_PRONOUN_PATTERNS = [
    (r"\b(I|I've|I'm|I'd|I'll|I’m|I’ve|I’d|I’ll)\b", "first-person 'I'"),
    (r"\b(my|me|mine|myself)\b", "first-person 'my/me'"),
    (r"\b(we|us|our|ours|ourselves)\b", "first-person plural"),
    (r"\b(you|your|yours|yourself)\b", "second-person"),
]


def _scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::test::canonicalizer_pipeline",
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


def _call_meta_data_add(
    *,
    source_sentence: str,
    utterance_date: str,
    node_label: str,
    node_type: str,
) -> dict[str, Any]:
    agent = DI.agent_factory.create_agent(META_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {META_AGENT!r}")

    node_payload = [{
        "temp_id": "n1",
        "node_type": node_type,
        "label": node_label,
        "category": "",
        "sentence": source_sentence,
    }]
    msg = Message(
        agent_input={
            "nodes": json.dumps(node_payload, ensure_ascii=True),
            "resolved_sentence": source_sentence,
            "message_timestamp": utterance_date,
        },
        scope_context=_scope(),
    )
    result = agent.action_handler(msg)
    data = getattr(result, "data", None) or {}
    nodes = data.get("Nodes") or []
    if not nodes:
        return {}
    return nodes[0]


def _call_canonicalizer(
    *,
    source_sentence: str,
    node_label: str,
    node_type: str,
    participants: Optional[str],
    enriched: dict[str, Any],
) -> str:
    agent = DI.agent_factory.create_agent(CANONICALIZER_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {CANONICALIZER_AGENT!r}")

    agent_input = {
        "extractor_sentence": source_sentence,
        "node_label": node_label,
        "node_type": node_type,
    }
    if participants:
        agent_input["participants"] = participants
    if enriched.get("start_date"):
        agent_input["start_date"] = enriched["start_date"]
    if enriched.get("end_date"):
        agent_input["end_date"] = enriched["end_date"]
    if enriched.get("valid_during"):
        agent_input["valid_during"] = enriched["valid_during"]

    result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    data = getattr(result, "data", None) or {}
    return data.get("canonical_sentence") or ""


def _date_close(actual: str, expected_iso: str, tolerance_days: int = 31) -> bool:
    """Allow some slop on relative-phrase resolution (date math may pick the
    1st-of-month vs the exact day, or month-vs-day-precision differences)."""
    if not actual or not expected_iso:
        return False
    from datetime import datetime
    try:
        a = datetime.fromisoformat(actual.split("T")[0])
        e = datetime.fromisoformat(expected_iso.split("T")[0])
    except ValueError:
        return False
    return abs((a - e).days) <= tolerance_days


def _run_case(case: dict) -> dict:
    name = case["name"]
    source = case["source"]
    utterance = case["utterance"]
    node_label = case["node_label"]
    node_type = case["node_type"]
    participants = case.get("participants")

    try:
        enriched = _call_meta_data_add(
            source_sentence=source,
            utterance_date=utterance,
            node_label=node_label,
            node_type=node_type,
        )
    except Exception as e:
        return {"name": name, "passed": False, "error": f"meta_data_add: {type(e).__name__}: {e}"}

    try:
        canonical = _call_canonicalizer(
            source_sentence=source,
            node_label=node_label,
            node_type=node_type,
            participants=participants,
            enriched=enriched,
        )
    except Exception as e:
        return {"name": name, "passed": False, "error": f"canonicalizer: {type(e).__name__}: {e}"}

    # Date assertions
    date_problems: list[str] = []
    expected_start = case.get("expected_start_date")
    expect_empty_start = case.get("expect_empty_start", False)
    actual_start = enriched.get("start_date") or ""

    if expect_empty_start:
        if actual_start.strip():
            date_problems.append(f"expected empty start_date, got {actual_start!r}")
    elif expected_start:
        if not _date_close(actual_start, expected_start):
            date_problems.append(
                f"expected start_date≈{expected_start}, got {actual_start!r}"
            )

    expected_end = case.get("expected_end_date")
    expect_empty_end = case.get("expect_empty_end", False)
    actual_end = enriched.get("end_date") or ""
    if expect_empty_end:
        if actual_end.strip():
            date_problems.append(f"expected empty end_date, got {actual_end!r}")
    elif expected_end:
        if not _date_close(actual_end, expected_end):
            date_problems.append(
                f"expected end_date≈{expected_end}, got {actual_end!r}"
            )

    # Canonicalizer assertions
    canonical_problems: list[str] = []
    if not canonical.strip():
        canonical_problems.append("empty canonical_sentence")
    else:
        temporal_hits = _check_violations(canonical, _TEMPORAL_PATTERNS)
        if temporal_hits:
            canonical_problems.append(f"temporal: {temporal_hits}")
        if participants:
            pronoun_hits = _check_violations(canonical, _PRONOUN_PATTERNS)
            if pronoun_hits:
                canonical_problems.append(f"pronouns: {pronoun_hits}")

    passed = not date_problems and not canonical_problems

    return {
        "name": name,
        "source": source,
        "utterance": utterance,
        "enriched": {
            "start_date": actual_start,
            "start_date_confidence": enriched.get("start_date_confidence", ""),
            "end_date": actual_end,
            "end_date_confidence": enriched.get("end_date_confidence", ""),
            "valid_during": enriched.get("valid_during", ""),
            "start_date_prose": enriched.get("start_date_prose", ""),
        },
        "canonical": canonical,
        "date_problems": date_problems,
        "canonical_problems": canonical_problems,
        "passed": passed,
    }


# --- test cases ---
# Utterance is 2026-04-22 throughout so relative phrase math is uniform.
_UTTER = "2026-04-22"

_CASES = [
    dict(
        name="married_2_years_ago",
        source="I got married 2 years ago.",
        utterance=_UTTER,
        node_label="Marriage",
        node_type="State",
        participants="Jukka, Katy",
        expected_start_date="2024-04-22",
    ),
    dict(
        name="bonnie_age_point_observation",
        source="Bonnie is about 3-4 years old.",
        utterance="2024-12-30",
        node_label="Age",
        node_type="State",
        participants="Bonnie",
        # Per the principle: present-tense observation with no duration anchor
        # should leave start_date empty and use valid_during.
        expect_empty_start=True,
        expect_empty_end=True,
    ),
    dict(
        name="diet_4_months_ongoing",
        source="I have been dieting for 4 months.",
        utterance=_UTTER,
        node_label="Diet",
        node_type="State",
        participants="Jukka",
        expected_start_date="2025-12-22",  # ~4 months before utterance
        expect_empty_end=True,
    ),
    dict(
        name="future_age_50",
        source="In 3 years I will be 50.",
        utterance=_UTTER,
        node_label="Age",
        node_type="State",
        participants="Jukka",
        expected_start_date="2029-04-22",
    ),
    dict(
        name="laptop_yesterday_event",
        source="Yesterday I bought a new MacBook Pro.",
        utterance=_UTTER,
        node_label="Laptop purchase",
        node_type="Event",
        participants="Jukka",
        expected_start_date="2026-04-21",
        expected_end_date="2026-04-21",  # single-day event
    ),
    dict(
        name="katy_google_6_years",
        source="Katy has been working at Google for 6 years.",
        utterance=_UTTER,
        node_label="Employment",
        node_type="State",
        participants="Katy, Google",
        expected_start_date="2020-04-22",
        expect_empty_end=True,
    ),
    dict(
        # Belief State about a future condition. The Belief is held NOW,
        # so meta_data_add should anchor start_date near the utterance
        # (or leave empty + valid_during="ongoing") — NOT 2028 (the
        # believed future), which is content-internal not validity.
        # Canonicalizer should produce a present-tense "believes" sentence
        # and not re-anchor on "in 2 years" as if that were utterance-relative
        # to the rendering moment.
        name="future_belief_bald",
        source="I believe in 2 years I will be bald.",
        utterance=_UTTER,
        node_label="Belief",
        node_type="State",
        participants="Jukka",
    ),
]


def main() -> int:
    print(f"=== fact_canonicalizer + meta_data_add pipeline test ({len(_CASES)} cases) ===\n")
    results: list[dict] = []
    for case in _CASES:
        r = _run_case(case)
        results.append(r)

        verdict = "PASS" if r.get("passed") else "FAIL"
        print(f"[{verdict}] {r['name']}")
        if "error" in r:
            print(f"   ERROR: {r['error']}")
            print()
            continue
        print(f"   src:       {r['source']}")
        print(f"   utterance: {r['utterance']}")
        e = r["enriched"]
        print(
            f"   meta:      start_date={e['start_date']!r} ({e['start_date_confidence']!r})  "
            f"end_date={e['end_date']!r} ({e['end_date_confidence']!r})  "
            f"valid_during={e['valid_during']!r}"
        )
        if e.get("start_date_prose"):
            print(f"              start_date_prose={e['start_date_prose']!r}")
        print(f"   canonical: {r['canonical']!r}")
        if r["date_problems"]:
            print(f"   date issues:      {r['date_problems']}")
        if r["canonical_problems"]:
            print(f"   canonical issues: {r['canonical_problems']}")
        print()

    n_pass = sum(1 for r in results if r.get("passed"))
    print(f"=== {n_pass}/{len(_CASES)} cases passed ===")
    return 0 if n_pass == len(_CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
