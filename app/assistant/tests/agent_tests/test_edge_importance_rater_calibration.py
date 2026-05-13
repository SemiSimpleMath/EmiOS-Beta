"""Durable calibration tests for ``me::edge_importance_rater``.

These hit the live agent (LLM cost) and check that representative edge
inputs produce scores within expected bands. The rater is non-deterministic
so we assert RANGES, not exact scores. Bands are wide enough that the
test fails only when calibration meaningfully drifts.

Append new test cases to ``CALIBRATION_CASES`` whenever you find a class
of edges the rater handles correctly (or incorrectly) and want to lock
the behavior. Each case is self-contained text — no DB dependency.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/agent_tests/test_edge_importance_rater_calibration.py -v
"""
from __future__ import annotations

import pytest

import app.assistant.tests.test_setup  # noqa: F401


# Each case represents one edge fed to the rater. Use the EXACT format
# the rater expects (mirror `app.assistant.kg.edge_importance._build_edge_block`).
# expected_min/max are the score bounds — score must satisfy
# expected_min <= score <= expected_max.
CALIBRATION_CASES = [
    # ====== DOCUMENT / LIST / TASK edges — should be LOW (1-3) ======
    {
        "name": "shopping_list_section_ordering",
        "input": (
            "id: test-1\n"
            "source label: Ralph's Shopping List\n"
            "edge label: has_state\n"
            "target label: Section Order\n"
            "source sentence: -\n"
            "target sentence: -\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "The first section on Ralph's shopping list is the deli/sushi section."
        ),
        "expected_min": 0.0,
        "expected_max": 3.5,
        "rationale": "Document structure detail — should rate 1-3 per prompt rules",
    },
    {
        "name": "shopping_list_grocery_item",
        "input": (
            "id: test-2\n"
            "source label: Ralph's Shopping List\n"
            "edge label: has_state\n"
            "target label: Chicken List Entry\n"
            "source sentence: -\n"
            "target sentence: -\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Ralph's shopping list includes either a whole rosemary/garlic roasted chicken "
            "or fried chicken pieces from the chicken case."
        ),
        "expected_min": 0.0,
        "expected_max": 3.5,
        "rationale": "Specific grocery item on a list — document content, low importance",
    },
    {
        "name": "task_load_document",
        "input": (
            "id: test-3\n"
            "source label: Document Loading\n"
            "edge label: targets\n"
            "target label: Ralph's Shopping List\n"
            "source sentence: -\n"
            "target sentence: -\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka asked to load Ralph's shopping list."
        ),
        "expected_min": 0.0,
        "expected_max": 3.5,
        "rationale": "One-off task interaction with a document",
    },

    # ====== FAMILY / IDENTITY bonds — should be HIGH (9-10) ======
    {
        "name": "spouse_marriage",
        "input": (
            "id: test-4\n"
            "source label: Jukka Virtanen\n"
            "edge label: is_spouse_in\n"
            "target label: Marriage\n"
            "source sentence: Jukka is the user, software engineer, family-centered.\n"
            "target sentence: Marriage of Jukka and Katy since September 9, 2003.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka is married to Katy since September 9, 2003."
        ),
        "expected_min": 8.5,
        "expected_max": 10.0,
        "rationale": "Marriage is identity-defining family bond",
    },
    {
        "name": "parent_of_child",
        "input": (
            "id: test-5\n"
            "source label: Jukka Virtanen\n"
            "edge label: is_parent_in\n"
            "target label: Parenthood\n"
            "source sentence: Jukka is the user, software engineer, family-centered.\n"
            "target sentence: Parenthood relating Jukka to his son Peter.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka Virtanen is Peter's father."
        ),
        "expected_min": 8.5,
        "expected_max": 10.0,
        "rationale": "Parent-child is identity-defining family bond",
    },

    # ====== PET — should be HIGH per updated prompt (was 5-7, now 8-10) ======
    {
        "name": "beloved_pet_dog",
        "input": (
            "id: test-6\n"
            "source label: Jukka Virtanen\n"
            "edge label: has_pet\n"
            "target label: Bonnie\n"
            "source sentence: Jukka loves his dogs and walks them daily.\n"
            "target sentence: Bonnie is one of Jukka and Katy's beloved family dogs.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Bonnie is one of Jukka and Katy's beloved dogs; they walk her daily and she sleeps inside."
        ),
        "expected_min": 7.5,
        "expected_max": 10.0,
        "rationale": "Beloved pet — per updated prompt should be 9-10; allow 7.5+ for safety",
    },

    # ====== HOBBY / IDENTITY interest — should be 7-8 per updated prompt ======
    {
        "name": "passionate_hobby_chess",
        "input": (
            "id: test-7\n"
            "source label: Jukka Virtanen\n"
            "edge label: has_state\n"
            "target label: Chess Hobby\n"
            "source sentence: Jukka is a software engineer who plays chess regularly.\n"
            "target sentence: Jukka's chess hobby — he plays online daily and identifies as a chess player.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka is really into chess; he plays online daily and identifies as a chess player."
        ),
        "expected_min": 6.0,
        "expected_max": 9.0,
        "rationale": "Identity-shaping hobby — 7-8 per updated prompt; allow 6-9 for noise",
    },
    {
        "name": "professional_identity_role",
        "input": (
            "id: test-8\n"
            "source label: David Weisbart\n"
            "edge label: has_state\n"
            "target label: Teaching Career\n"
            "source sentence: David Weisbart is a math professor at UC Riverside.\n"
            "target sentence: Teaching is central to David's professional identity.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "David Weisbart is really into teaching — it's central to his professional identity as a math professor."
        ),
        "expected_min": 7.0,
        "expected_max": 10.0,
        "rationale": "Identity-defining professional role — 8-10 per updated prompt",
    },

    # ====== ONE-OFF mention — should be LOW (1-3) ======
    {
        "name": "one_off_attendance",
        "input": (
            "id: test-9\n"
            "source label: Jukka Virtanen\n"
            "edge label: attended\n"
            "target label: Conference\n"
            "source sentence: Jukka is a software engineer.\n"
            "target sentence: A one-time conference Jukka happened to attend.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka attended a one-day AI conference in March 2024."
        ),
        "expected_min": 0.0,
        "expected_max": 4.0,
        "rationale": "Single-day attendance — 1-3 per prompt rules",
    },

    # ====== GOAL edges — span the full range ======
    {
        "name": "goal_long_term_identity",
        "input": (
            "id: test-10\n"
            "source label: Jukka Virtanen\n"
            "edge label: has_goal\n"
            "target label: Raise Good Children\n"
            "source sentence: Jukka is a father of Peter and Annika.\n"
            "target sentence: Long-term life goal of raising children to become good adults.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka's goal is to raise his children to become good adults."
        ),
        "expected_min": 7.0,
        "expected_max": 10.0,
        "rationale": "Identity-defining life goal — 8+ per prompt level-8 example",
    },
    {
        "name": "goal_multi_year_active",
        "input": (
            "id: test-11\n"
            "source label: Jukka Virtanen\n"
            "edge label: has_goal\n"
            "target label: 2000 Chess Rating\n"
            "source sentence: Jukka plays chess regularly.\n"
            "target sentence: Multi-year goal to reach a 2000 chess.com rating.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka would like to reach a 2000 chess rating before he dies."
        ),
        "expected_min": 6.0,
        "expected_max": 9.0,
        "rationale": "Multi-year active goal tied to hobby identity — 7 per prompt level-7 example",
    },
    {
        "name": "goal_transient_momentary",
        "input": (
            "id: test-12\n"
            "source label: Jukka Virtanen\n"
            "edge label: has_goal\n"
            "target label: Glass of Milk\n"
            "source sentence: Jukka is the user.\n"
            "target sentence: Transient momentary craving for milk.\n"
            "EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: "
            "Jukka wishes he had a big cold glass of milk right now."
        ),
        "expected_min": 0.0,
        "expected_max": 2.5,
        "rationale": "Momentary craving treated as transient goal — 1 per prompt level-1 example",
    },
]


@pytest.fixture(scope="module")
def rater():
    from app.assistant.ServiceLocator.service_locator import DI
    agent = DI.agent_factory.create_agent("me::edge_importance_rater")
    if agent is None:
        pytest.skip("me::edge_importance_rater agent unavailable")
    return agent


@pytest.fixture(scope="module")
def scope():
    from app.assistant.utils.pydantic_classes import (
        ScopeApprovalPolicy, ScopeContext, ScopeResourcePolicy,
    )
    return ScopeContext(
        scope_id="scope::test::edge_rater",
        owner_id="jukka",
        actor_id="edge_rater_calibration_test",
        surface="ui",
        room_id="me_lens",
        approval=ScopeApprovalPolicy(authority_level=99),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


@pytest.fixture(scope="module")
def all_ratings(rater, scope):
    """Run the rater once on all calibration cases (single batch).

    Module-scoped so the LLM call only fires once for the whole suite.
    Returns dict: case_name -> {'score': float, 'reason': str}.
    """
    from app.assistant.utils.pydantic_classes import Message

    # Build the batch input — mirror the format the prod path uses.
    blocks = [case["input"] for case in CALIBRATION_CASES]
    batch_text = "\n\n".join(blocks)

    msg = Message(
        agent_input={"task": batch_text, "information": ""},
        task=batch_text,
        information="",
        scope_context=scope,
    )
    result = rater.action_handler(msg)
    data = getattr(result, "data", None) or {}
    if hasattr(data, "model_dump"):
        data = data.model_dump()

    ratings_list = data.get("ratings") or []
    # Build name -> rating lookup by ID. Each test case's id is its position
    # ("test-1", "test-2", ...) since we set those in the input blocks.
    by_id: dict[str, dict] = {}
    for r in ratings_list:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        if rid:
            by_id[rid] = r

    out: dict[str, dict] = {}
    for i, case in enumerate(CALIBRATION_CASES, start=1):
        rid = f"test-{i}"
        rating = by_id.get(rid)
        if rating is None:
            out[case["name"]] = {"score": None, "reason": "(no rating returned)"}
        else:
            out[case["name"]] = {
                "score": rating.get("score"),
                "reason": rating.get("reason") or "",
            }
    return out


@pytest.mark.parametrize(
    "case",
    CALIBRATION_CASES,
    ids=[c["name"] for c in CALIBRATION_CASES],
)
def test_edge_rating_within_band(case, all_ratings):
    """Each case's score must fall within [expected_min, expected_max]."""
    result = all_ratings.get(case["name"])
    assert result is not None, f"no result for {case['name']}"
    score = result["score"]
    assert score is not None, f"rater returned no score for {case['name']}: reason={result['reason']!r}"
    score = float(score)
    lo, hi = case["expected_min"], case["expected_max"]
    assert lo <= score <= hi, (
        f"{case['name']}: score {score} not in [{lo}, {hi}]\n"
        f"  rationale: {case['rationale']}\n"
        f"  rater reason: {result['reason']!r}"
    )
