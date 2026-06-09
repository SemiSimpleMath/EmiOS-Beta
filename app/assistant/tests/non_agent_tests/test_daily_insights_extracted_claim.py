"""Belief-engine v2 ingestion seam (§3a): the daily_insights extractor enrichment must be ADDITIVE.

ActionableItem gains an OPTIONAL structured extracted_claim. These tests pin that the live nightly
pipeline can't break: items WITHOUT extracted_claim still validate (old stored items / older agent
output), items WITH a full claim validate, and the claim's soft fields default correctly (qualifiers
empty, applies_when None, polarity 'affirm') per the spec guardrails.
"""
from __future__ import annotations

from app.assistant.agents.daily_timeline_insights.agent_form import (
    ActionableItem,
    AgentForm,
    ExtractedClaim,
)


def test_item_validates_without_extracted_claim():
    # Backward compatibility: the field is optional, so old payloads (no extracted_claim) still load.
    item = ActionableItem(
        fact_summary="The user spaces out standing-break nudges.",
        tags=["routine"],
        change_recommended="Reduce standing-break cadence.",
        evidence=["snoozed standing-break 3x"],
    )
    assert item.extracted_claim is None
    assert item.temporal_scope == "chronic"


def test_item_validates_with_full_claim():
    item = ActionableItem(
        fact_summary="The user finds standing-break nudges too frequent.",
        tags=["routine"],
        change_recommended="Space them out.",
        evidence=["snoozed 3x"],
        extracted_claim=ExtractedClaim(
            subject_phrase="the user",
            predicate_phrase="finds too frequent",
            object_phrase="standing-break nudges",
            qualifiers=["during deep work"],
            applies_when="weekday mornings",
            polarity="affirm",
            llm_interpretation="reduce break nudge cadence",
        ),
    )
    assert item.extracted_claim.object_phrase == "standing-break nudges"
    assert item.extracted_claim.qualifiers == ["during deep work"]


def test_claim_soft_field_defaults():
    claim = ExtractedClaim(
        subject_phrase="the kids", predicate_phrase="won't eat", object_phrase="zucchini",
    )
    assert claim.qualifiers == []
    assert claim.applies_when is None
    assert claim.polarity == "affirm"
    assert claim.llm_interpretation is None


def test_agent_form_roundtrip_mixed():
    # A batch with one structured + one bare item validates and round-trips through JSON.
    form = AgentForm(actionable_information=[
        ActionableItem(fact_summary="a", tags=[], change_recommended="x"),
        ActionableItem(
            fact_summary="b", tags=[], change_recommended="y",
            extracted_claim=ExtractedClaim(
                subject_phrase="the user", predicate_phrase="prefers",
                object_phrase="coffee before 11am",
            ),
        ),
    ])
    back = AgentForm.model_validate(form.model_dump())
    assert back.actionable_information[0].extracted_claim is None
    assert back.actionable_information[1].extracted_claim.predicate_phrase == "prefers"
