"""Relevance is selected by an LLM; kind/tag metadata must not hide context."""
from __future__ import annotations

import app.assistant.pipelines.dayflow.steps.dayflow_routine_stage as drs


def test_routine_block_preserves_all_active_kinds_for_selection():
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
    assert "Home address baseline fact" in out
    # Preferences and episodic context remain available to the relevance agent.
    assert "Avoid mustard" in out
    assert "Cancel the Coursera" in out
    assert "A deprecated routine" not in out


def test_routine_block_admits_routine_tagged_regardless_of_kind():
    """Tags provide context but do not hide untagged or differently tagged preferences."""
    entries = [
        {"belief_key": "routine.ac_morning", "domain": "routine", "kind": "stable_preference",
         "tags": ["home_automation", "routine", "schedule"], "status": "active",
         "statement": "Default cooling 70F at 21:00, then 75F at 06:00 so the AC stops in the morning."},
        {"belief_key": "food.no_mustard", "domain": "food", "kind": "stable_preference",
         "tags": ["food"], "status": "active", "statement": "Avoid mustard on family sandwiches."},
    ]
    out = drs._render_belief_block(entries)
    assert "75F at 06:00" in out
    assert "Avoid mustard" in out
