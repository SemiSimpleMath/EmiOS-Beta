"""belief_engine.intake.recall.rank — the pick-from-candidates step, on invented beliefs. No DB, no model."""
from datetime import date

from belief_engine.intake.recall import format_for_prompt, rank

TODAY = date(2026, 9, 27)


def belief(i, statement, vec, last_day="2026-09-20", support=3):
    return {"id": f"B{i}", "statement": statement, "kind": "stable_preference", "embedding": vec,
            "first_day": "2026-02-10", "last_day": last_day, "support": support}


def test_closest_in_meaning_ranks_first_and_k_cuts():
    cands = [belief(1, "A likes tea", [1.0, 0.0, 0.0]),
             belief(2, "A likes herbal tea at night", [0.6, 0.8, 0.0]),
             belief(3, "A prefers a standing desk", [0.0, 0.0, 1.0])]
    out = rank(cands, [0.9, 0.1, 0.0], [], k=2, today=TODAY)
    assert [b["id"] for b in out] == ["B1", "B2"]


def test_a_named_person_lifts_a_belief_about_them():
    # B2 is a little further from the message than B1; naming Kim lifts it past B1.
    cands = [belief(1, "A likes tea", [1.0, 0.0]),
             belief(2, "Kim takes the dogs out in the morning", [0.95, 0.31])]
    assert rank(cands, [1.0, 0.0], [], k=1, today=TODAY)[0]["id"] == "B1"
    assert rank(cands, [1.0, 0.0], ["Kim"], k=1, today=TODAY)[0]["id"] == "B2"
    # The lift is bounded: a belief naming the person still needs closeness in meaning.
    far = belief(3, "Kim collects stamps", [0.0, 1.0])
    assert rank(cands + [far], [1.0, 0.0], ["Kim"], k=1, today=TODAY)[0]["id"] != "B3"


def test_name_match_is_whole_word():
    cands = [belief(1, "A dislikes Kimchi", [1.0, 0.0]), belief(2, "Kim likes pasta", [1.0, 0.0])]
    out = rank(cands, [1.0, 0.0], ["Kim"], k=2, today=TODAY)
    assert out[0]["id"] == "B2"


def test_recent_and_often_observed_beat_stale_at_equal_closeness():
    cands = [belief(1, "A likes tea", [1.0, 0.0], last_day="2026-03-01", support=1),
             belief(2, "A likes coffee", [1.0, 0.0], last_day="2026-09-25", support=8)]
    out = rank(cands, [1.0, 0.0], [], k=2, today=TODAY)
    assert [b["id"] for b in out] == ["B2", "B1"]


def test_k_is_a_ceiling_and_unrelated_beliefs_stay_out():
    cands = [belief(1, "A likes tea", [1.0, 0.0]),
             belief(2, "A prefers a standing desk", [0.1, 1.0]),
             belief(3, "A walks the dogs at night", [0.0, 1.0])]
    out = rank(cands, [1.0, 0.0], [], k=10, today=TODAY)
    assert [b["id"] for b in out] == ["B1"]
    assert rank(cands, [0.0, 1.0], [], k=10, today=TODAY) and all(b["id"] != "B1" for b in rank(cands, [0.0, 1.0], [], k=10, today=TODAY))


def test_prompt_lines_carry_statement_and_date():
    text = format_for_prompt([{"statement": "A likes tea", "last_day": "2026-09-20"}])
    assert text == "- A likes tea (last seen 2026-09-20)"
