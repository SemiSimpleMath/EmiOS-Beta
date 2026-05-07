from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class GoalOutcomeVerdict(BaseModel):
    """Verdict on whether one Goal has been achieved, abandoned, or
    neither based on recent chat evidence."""

    goal_node_id: str = Field(
        description="Exact id of the Goal node being judged. Must match a candidate id.",
    )

    outcome: Literal["completed", "abandoned", "no_signal"] = Field(
        description=(
            "completed: the user said the goal was finished / accomplished / "
            "done / achieved. abandoned: the user said they're giving up / "
            "no longer pursuing / not interested anymore. no_signal: nothing "
            "in the chat evidence points clearly either way; leave the Goal "
            "alone. The vocabulary 'completed' aligns with the existing "
            "goal_status column values; 'achieved' would be a synonym but "
            "non-canonical."
        ),
    )

    evidence_quote: Optional[str] = Field(
        default=None,
        description=(
            "When outcome is achieved or abandoned: verbatim chat-message "
            "fragment that proves it. Must be quoted from the candidate's "
            "evidence; do not paraphrase. Null when outcome=no_signal."
        ),
    )

    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Strength of the signal. >= 0.85 means 'unambiguous' (direct "
            "quote like 'I finished it' or 'I'm not doing this anymore'). "
            "0.6-0.85 means 'strong inference' (e.g. user moved on to a "
            "new related goal). < 0.6: don't emit a non-no_signal verdict; "
            "set outcome=no_signal instead."
        ),
    )

    reasoning: str = Field(
        description="One sentence: why this outcome, citing the evidence_quote.",
    )


class AgentForm(BaseModel):
    """Output of goal_outcome_detector.

    `verdicts` is one entry per candidate Goal that the agent was asked
    to judge. Goals where chat evidence is ambiguous get
    outcome='no_signal' — the dormancy sweep will eventually retire
    them; only explicit signals close them here.
    """

    verdicts: List[GoalOutcomeVerdict]
    reason: str = Field(
        description=(
            "One-sentence summary. 'Detected 1 achievement, 1 abandonment, "
            "8 no-signal' or similar."
        ),
    )
