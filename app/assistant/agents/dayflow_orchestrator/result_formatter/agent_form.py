from typing import List, Optional

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Output of dayflow_orchestrator::result_formatter.

    This agent reads the FULL manager-result text (untruncated) and
    emits a compact, specifics-preserving task-update line that the
    strategic_planner can consume on its next tick. The full report
    stays in audit (action_log) for any later agent that wants to
    drill in.

    Hard rule: the agent MUST receive the full untruncated source.
    Char-slicing the input upstream defeats the purpose. The agent
    is the only place that decides what's important.
    """

    outcome: str = Field(
        description=(
            "1-3 lines describing what the manager actually accomplished "
            "or could not accomplish. Preserve concrete specifics — "
            "dates, IDs, addresses, error codes, named entities, dollar "
            "amounts, time windows. Lead with the verdict ('Found', "
            "'Closed', 'Could not verify', 'Sent', etc.). No "
            "editorializing, no hedging boilerplate."
        ),
    )

    open_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Specific questions the manager surfaced that the planner "
            "should be aware of (e.g. 'Delivery window not in email; "
            "tracking arrives later'). Empty when the result was self-"
            "contained."
        ),
    )

    needs_followup: bool = Field(
        description=(
            "True iff the manager's report explicitly suggests further "
            "work the planner should consider scheduling. Don't infer; "
            "only set when the manager said so."
        ),
    )

    followup_hint: Optional[str] = Field(
        default=None,
        description=(
            "When needs_followup is True: one-sentence hint of what "
            "the next step should be. E.g. 'Check tracking link after "
            "package ships.' Null otherwise."
        ),
    )
