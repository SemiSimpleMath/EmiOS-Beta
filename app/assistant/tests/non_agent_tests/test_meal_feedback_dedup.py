"""Dish-level dedup for meal-feedback questions.

Regression for: the assistant asked "how was the salmon?" over and over because the meal
plan reuses last night's salmon as leftover lunches, and each meal-plan slot
minted its own feedback question. The dish matcher must collapse a dish and its
leftovers to ONE dish (so the producer skips re-asking, and answering one cancels
the same-dish siblings)."""
from app.assistant.subconscious.meal_feedback_runner import _dish_tokens, _same_dish

SALMON_VARIANTS = [
    "Baked salmon with broccoli and potatoes (low-effort oven bake)",
    "Oven-baked salmon with roasted broccoli and potatoes (simple lemon + olive oil)",
    "Leftovers: Baked salmon with broccoli and potatoes (lunch on 2026-06-09)",
    "Leftover baked salmon with broccoli and potatoes - reheated, packed (lunch)",
]


def test_salmon_dinner_and_its_leftovers_are_one_dish():
    base = _dish_tokens(SALMON_VARIANTS[0])
    for v in SALMON_VARIANTS[1:]:
        assert _same_dish(base, _dish_tokens(v)), f"should match base salmon: {v!r}"
    # every variant reduces to the same core ingredients (method/leftover noise stripped)
    for v in SALMON_VARIANTS:
        assert _dish_tokens(v) == frozenset({"salmon", "broccoli", "potatoes"}), v


def test_leftover_chicken_matches_its_dinner():
    a = _dish_tokens("Chicken & broccoli stir-fry with carrots and rice")
    b = _dish_tokens("Leftover chicken and broccoli stir-fry over rice (no onions)")
    assert _same_dish(a, b)


def test_distinct_dishes_do_not_match():
    salmon = _dish_tokens("Baked salmon with broccoli and potatoes")
    assert not _same_dish(salmon, _dish_tokens("Bagels with cream cheese and bananas"))
    assert not _same_dish(salmon, _dish_tokens("Quick yogurt parfaits with berries and chia"))
    # shares only 'broccoli' — one common ingredient must NOT collapse two dishes
    assert not _same_dish(salmon, _dish_tokens("Chicken and broccoli stir-fry over rice"))
