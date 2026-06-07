"""Guard: every meal/shopping/weekly-plan pod actually mints.

Two NameErrors once lived in meal_persist's mint helpers (an undefined
`addresses` in the meal mint, an undefined `agent_name` in the shopping mint).
Because the mints swallowed exceptions, the daily proposer ran "green" while
silently dropping EVERY meal pod and EVERY shopping pod — see
scratch/MEAL-PLANNING-AUDIT.md.

These tests drive the PUBLIC apply_* entry points (the same calls the routines
make) with an in-memory PodStore, so they exercise pod construction end-to-end
without touching the DB. A NameError in any mint now fails the build here rather
than in production. This file is in the pre-push regression guard.

Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/non_agent_tests/test_meal_pod_mint_guard.py
"""
from __future__ import annotations

from app.assistant.subconscious import meal_persist


class _FakePodStore:
    """Captures Pod objects instead of writing them to SQLite."""

    def __init__(self):
        self.pods = []

    def put(self, pod):
        self.pods.append(pod)


def _kinds(store):
    return [getattr(p, "kind", None) for p in store.pods]


def test_daily_proposer_mints_meal_shopping_and_set_pods(monkeypatch):
    store = _FakePodStore()
    monkeypatch.setattr(meal_persist, "PodStore", lambda: store)

    output = {
        "proposals": [
            {
                "actors": ["Owner"],
                "dish": "Sheet-pan chicken & veg",
                "meal_window": "dinner",
                "date": "2026-06-08",
                "source": "home_cook",
                "novelty": "familiar",
                "confidence": "high",
                "reasoning": "Quick weeknight staple.",
                "primary_ingredients": ["chicken thighs", "broccoli", "potatoes"],
                "needs_shopping": ["broccoli"],
            }
        ],
        "shopping_run": {
            "items": ["broccoli", "milk", "eggs"],
            "suggested_date": "2026-06-08",
            "reasoning": "Restock for the week.",
        },
        "fast_food_advisory": None,
        "free_form_thinking": "Lean on what's already in the pantry.",
        "skipped_meals": [],
    }

    summary = meal_persist.apply_daily_meal_proposer_output(output)

    # The meal mint must NOT silently drop the proposal (the `addresses` NameError did).
    assert summary["proposal_pod_count"] == 1, summary
    assert summary["proposal_pod_ids"], summary
    # The shopping mint must NOT silently drop the run (the `agent_name` NameError did).
    assert summary["shopping_pod_id"], summary
    assert summary["set_pod_id"], summary

    # One meal + one shopping + one set pod, all stamped with the calling agent.
    assert _kinds(store) == ["intention.meal", "intention.shopping", "intention.meal_set"]
    assert all(p.created_by == "daily_meal_proposer" for p in store.pods)


def test_weekly_planner_mints_plan_pod(monkeypatch):
    store = _FakePodStore()
    monkeypatch.setattr(meal_persist, "PodStore", lambda: store)

    output = {
        "weekly_plan": {
            "week_start_date": "2026-06-08",
            "week_theme": "Pantry-first, low-effort weeknights",
            "slots": [
                {
                    "date": "2026-06-08",
                    "day_of_week": "Monday",
                    "meal_window": "dinner",
                    "slot_type": "home_cook",
                    "dish": "Pasta puttanesca",
                }
            ],
        },
        # action=none keeps the Google-Doc shopping-list write out of the test.
        "weekly_shopping_list": {"action": "none"},
        "free_form_thinking": "",
    }

    summary = meal_persist.apply_weekly_meal_planner_output(output)

    assert summary["plan_pod_id"], summary
    assert _kinds(store) == ["plan.weekly_meals"]
    assert store.pods[0].created_by == "weekly_meal_planner"
