"""Output schema for skill_distiller.

The distiller reviews a week's worth of subconscious activity and
proposes additions to the household's learned-skill files. PROPOSES
ONLY — output is appended to resource_learned_skills_proposed.md
for user review. The user moves accepted lines into the canonical
files (priority_rules, house_rules) when they're ready.

Calibration goals:
- Each proposal grounded in concrete evidence (>=2 observations).
- Be conservative — one strong proposal beats five vague ones.
- Reject hypothesis when evidence is sparse; explicitly skipping is fine.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


TargetFile = Literal[
    "resource_scheduler_priority_rules",
    "resource_meal_proposer_house_rules",
    "resource_wellness_proposer_house_rules",
    "resource_romantic_proposer_house_rules",
    "resource_subconscious_noticer_house_rules",
    "resource_subconscious_watchlist",
    "resource_recurring_obligations",
]
ProposalKind = Literal[
    "priority_rule",
    "house_rule_calibration",
    "watchlist_addition",
    "recurring_obligation",
    "tone_calibration",
]
Confidence = Literal["high", "medium", "low"]


class EvidenceItem(BaseModel):
    """One observation grounding a proposal."""
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal[
        "intention.meal", "intention.wellness", "intention.romantic",
        "intention.shopping", "intention.wellness_set", "intention.romantic_set",
        "intention.meal_set",
        "plan.weekly_schedule", "concern", "chat_cluster", "ticket_answer",
        "user_data", "observation",
    ]
    ref: str = Field(description="Pod id, concern_id, ticket_id, or descriptive label.")
    snippet: str = Field(max_length=300, description="Short excerpt or summary.")


class LearnedRuleProposal(BaseModel):
    """One proposed rule addition for user review."""
    model_config = ConfigDict(extra="forbid")
    proposal_kind: ProposalKind
    target_file: TargetFile = Field(
        description="Which canonical file this should be appended to if approved.",
    )
    target_section: Optional[str] = Field(
        default=None,
        max_length=120,
        description="Markdown section header in the target file (best-effort match). e.g. 'Soft precedences' for priority_rules. Helps the user know where to paste it.",
    )
    rule_text: str = Field(
        max_length=500,
        description="The line(s) to append. Plain markdown — usually a bullet or short paragraph. Should be self-contained.",
    )
    why_now: str = Field(
        max_length=300,
        description="What changed this week that surfaced this proposal.",
    )
    evidence: List[EvidenceItem] = Field(
        description="At minimum 2 evidence items. Each ties to a real source.",
    )
    confidence: Confidence
    novelty: Literal["new_rule", "refines_existing", "contradicts_existing"]
    related_existing_rule: Optional[str] = Field(
        default=None,
        max_length=200,
        description="If novelty is refines/contradicts, quote the rule this is touching.",
    )


class AgentForm(BaseModel):
    """Top-level skill_distiller output for one weekly run."""
    model_config = ConfigDict(extra="forbid")
    week_window_label: str = Field(
        description="ISO label for the window this run covers (e.g. '2026-05-13_to_2026-05-20').",
    )
    proposals: List[LearnedRuleProposal] = Field(default_factory=list)
    skipped_reasons: List[str] = Field(
        default_factory=list,
        description="Explicit reasons why hypotheses were considered but NOT promoted (sparse evidence, contradiction with stronger existing rule, etc.). Keeps the distiller honest about not over-emitting.",
    )
    free_form_thinking: str = Field(
        max_length=1500,
        description="What the week looked like, what patterns emerged, what to watch for next week.",
    )
