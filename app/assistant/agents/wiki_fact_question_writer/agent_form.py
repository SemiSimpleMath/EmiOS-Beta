"""Output schema for wiki::fact_question_writer.

Gate + craft: decide whether a wiki-inferred fact proposal is worth
confirming with the user, and if so write the natural confirmation
question."""
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worthy: bool = Field(
        description=(
            "True when confirming this fact would durably enrich the personal "
            "knowledge graph (relationships, history, places, preferences, "
            "events). False for taxonomy trivia ('water is a drink'), "
            "definitional filler, or anything the graph obviously already "
            "implies."
        )
    )
    skip_reason: Optional[str] = Field(
        default=None, max_length=200,
        description="Required when worthy=false — why this isn't worth the user's time.",
    )
    question_text: Optional[str] = Field(
        default=None, max_length=300,
        description=(
            "Required when worthy=true. A natural, conversational confirmation "
            "question. Mention it came from connecting things ('Am I right "
            "that...', 'I pieced together that...'). ONE question, answerable "
            "with a yes/no/correction."
        ),
    )
    if_unanswered: Optional[str] = Field(
        default=None, max_length=200,
        description="Required when worthy=true. The default if the user never answers (usually: leave the graph unchanged).",
    )
