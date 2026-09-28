from __future__ import annotations

import json
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class EvidenceRelation(BaseModel):
    """Why one supplied observation belongs to one atom. Mirrors the belief_updater contract:
    a citation without a stated relation is rejected, and the relation is never inferred from
    whether the underlying event sounds positive or negative."""
    evidence_ref: int = Field(description="1-based index from the numbered evidence block.")
    valence: Literal["support", "contradict", "qualify"] = Field(
        description="How this observation bears on THIS atom — not on the original belief.")
    reasoning: str = Field(description="One sentence: what in this observation bears on this atom.")


class Atom(BaseModel):
    belief_key: str = Field(
        description=(
            "Stable dot-notation slug for this atom. EXACTLY ONE atom must reuse the original "
            "belief's key — the one carrying the claim closest to the original's centre of "
            "gravity — so the belief is never orphaned. Every other atom needs a new key that "
            "does not collide with an existing belief. Keys describe the claim, not the source.")
    )
    is_primary: bool = Field(
        description="True for the single atom that reuses the original key. Exactly one atom.")
    statement: str = Field(
        description=(
            "ONE claim, as a declarative statement of something true about the person or their "
            "world, stated so it stands alone. Any wish, judgment or rule names the person who "
            "holds it. A reader who sees only this sentence, with no "
            "access to the original belief or the evidence, understands what is true. Name who "
            "and what is meant. Keep every qualifier that belongs to THIS claim.")
    )
    kind: Literal[
        "durable_fact",
        "stable_relationship",
        "stable_preference",
        "routine_pattern",
        "episodic_context",
        "transient_state",
    ] = Field(
        description=(
            "Chosen PER ATOM — this is a main reason to split at all, because kind sets how fast "
            "the claim loses force:\n"
            "  durable_fact = a fact or a standing rule that should not fade (never decays)\n"
            "  stable_relationship = persistent ties (~5 yr)\n"
            "  stable_preference = enduring taste or preference (~1 yr)\n"
            "  routine_pattern = a recurring habit or schedule (~3 mo)\n"
            "  episodic_context = a bounded current period (~2 wk)\n"
            "  transient_state = a right-now status (~1 day)\n"
            "Choose by how long this atom stays useful to know. A dated completed event stays "
            "true but is relevant only for the days around it.")
    )
    scope: Literal["chronic", "temporary"] = Field(
        description="chronic = ongoing. temporary = bounded to days or weeks.")
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "How well THIS atom is supported by the observations you attached to it — not how "
            "confident the original belief was. An atom you could attach no evidence to is not "
            "high.")
    )
    conditions_json: Optional[str] = Field(
        default=None,
        description=(
            "JSON object, as text, for applicability that is genuinely structured — a time "
            "window, a day, a threshold, a place. Use it only when the condition is crisp. Do "
            "not restate the sentence here, and do not invent a condition to look precise; "
            "leave it null when the claim simply applies.")
    )
    evidence_refs: List[int] = Field(
        default_factory=list,
        description=(
            "Indices of the supplied observations that support THIS atom. An observation may "
            "support more than one atom — cite it in each. Cite only what genuinely bears on "
            "this claim: attaching the parent's whole trail to every atom gives each one "
            "confidence it did not earn.")
    )
    evidence_relations: List[EvidenceRelation] = Field(
        default_factory=list,
        description="One entry for every index in evidence_refs. No citation without a relation.")
    reasoning: str = Field(
        description="One or two sentences: why this is a separate claim and why this kind.")

    @field_validator("conditions_json")
    @classmethod
    def validate_conditions(cls, value):
        if value is not None and not isinstance(json.loads(value), dict):
            raise ValueError("conditions_json must encode an object")
        return value


class AgentForm(BaseModel):
    verdict: Literal["already_atomic", "split"] = Field(
        description=(
            "already_atomic = the belief carries one claim; return it as a single atom, improved "
            "where needed (references resolved, kind corrected, evidence cited). "
            "split = it carries more than one claim. Most beliefs are already atomic — say so. "
            "Splitting is not the goal; accuracy is.")
    )
    atoms: List[Atom] = Field(
        min_length=1,
        max_length=8,
        description=(
            "The belief rewritten as atoms, always at least one. Two to four is the normal range "
            "for a genuine split. If you find yourself past five, you are probably splitting a "
            "single claim into its grammar rather than into claims.")
    )
    unattached_evidence_refs: List[int] = Field(
        default_factory=list,
        description=(
            "Supplied observations that support NO atom you produced. Report them honestly — "
            "this is a check on the split, not a failure. Evidence with nowhere to go usually "
            "means a claim is missing, or the original belief rested on something it never "
            "actually said.")
    )
    reasoning: str = Field(
        description=(
            "Two or three sentences on the carving: what claims you found, what you deliberately "
            "kept together, and anything you could not resolve from the material given.")
    )
