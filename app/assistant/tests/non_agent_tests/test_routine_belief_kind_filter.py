"""Guard: the daily-routine writer only gets schedule-shaping beliefs.

The dayflow_routine_writer was fed all ~826 active beliefs, so the actual routine
items (a weekly timesheet, AC anchors) drowned under 416 background preferences +
101 one-off episodic facts. It now scopes to routine-relevant `kind`s
(routine_pattern + durable_fact). Importance rises organically from evidence
ranking — there is no hardcoded pin list.

Hermetic — renders from a fixture entry list, no live export file. Pre-push guard.
"""
from __future__ import annotations

import app.assistant.pipelines.dayflow.steps.dayflow_routine_stage as drs


def test_routine_block_scopes_to_routine_kinds():
    entries = [
        {"belief_key": "admin.timesheets.weekly_monday", "domain": "routine", "kind": "routine_pattern",
         "confidence": "high", "status": "active",
         "statement": "Weekly timesheets: Monday is timesheet day, target 10:00."},
        {"belief_key": "routine.ac_anchor", "domain": "routine", "kind": "routine_pattern",
         "confidence": "high", "status": "active", "statement": "Set AC to 70F at 21:00."},
        {"belief_key": "general.home_addr", "domain": "general", "kind": "durable_fact",
         "confidence": "high", "status": "active", "statement": "Home address baseline fact."},
        {"belief_key": "food.no_mustard", "domain": "food", "kind": "stable_preference",
         "confidence": "high", "status": "active", "statement": "Avoid mustard on family sandwiches."},
        {"belief_key": "general.coursera", "domain": "general", "kind": "episodic_context",
         "confidence": "medium", "status": "active", "statement": "Cancel the Coursera subscription."},
        {"belief_key": "routine.old", "domain": "routine", "kind": "routine_pattern",
         "confidence": "high", "status": "deprecated", "statement": "A deprecated routine."},
    ]
    out = drs._render_belief_block(entries)

    # routine-shaping kinds kept (incl. the symptom belief)
    assert "Weekly timesheets" in out
    assert "Set AC to 70F at 21:00" in out
    assert "Home address baseline fact" in out          # durable_fact kept
    # noise kinds dropped
    assert "Avoid mustard" not in out                   # stable_preference
    assert "Cancel the Coursera" not in out             # episodic_context
    assert "A deprecated routine" not in out            # deprecated never included


def test_routine_block_admits_routine_tagged_regardless_of_kind():
    """A recurring schedule that `kind` mislabels a preference (the 6 AM AC step-up) is still
    admitted when it carries a routine/home_automation TAG. `kind` is the decay axis; tags are
    the routing axis. Preferences WITHOUT a routine tag (mustard) still stay out."""
    entries = [
        {"belief_key": "routine.ac_morning", "domain": "routine", "kind": "stable_preference",
         "tags": ["home_automation", "routine", "schedule"], "status": "active",
         "statement": "Default cooling 70F at 21:00, then 75F at 06:00 so the AC stops in the morning."},
        {"belief_key": "food.no_mustard", "domain": "food", "kind": "stable_preference",
         "tags": ["food"], "status": "active", "statement": "Avoid mustard on family sandwiches."},
    ]
    out = drs._render_belief_block(entries)
    assert "75F at 06:00" in out          # stable_preference, but routine/home_automation-tagged -> admitted
    assert "Avoid mustard" not in out     # stable_preference, only food-tagged -> still dropped
