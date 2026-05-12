from typing import List, Optional

from pydantic import BaseModel, Field


class Node(BaseModel):
    """Temporal metadata for one State or Event node.

    Scoped to State + Event only (2026-05-12). Entity/Concept/Property nodes
    don't pass through this agent — they need nothing the agent can add.
    Goal nodes get validity from goal_status deterministically (no LLM).

    For values that aren't applicable / not known, emit empty string "" (or
    null for valid_currently). This keeps the structured-output schema strict
    and avoids the garbage-fragment failure mode when a required `str` field
    is told to "emit null".
    """
    temp_id: str

    start_date: str
    start_date_confidence: str
    # Natural-language start anchor when exact date is unknown but a
    # relational anchor exists ("shortly after Sam was born", "during
    # Waldo's last year"). Empty string "" when not applicable.
    start_date_prose: str

    end_date: str
    end_date_confidence: str
    end_date_prose: str

    # Asymmetric trust: only `false` is a meaningful write. The agent stamps
    # `false` ONLY when the window has explicit evidence the state/event ended
    # (a closing event, prose like "after moving to X" or "before 1990",
    # "when X was Y years old" + context that Y is past). Otherwise null.
    # We never claim `true` — absence of closing evidence isn't proof a
    # state is still in effect; a downstream reader assumes presumed-valid
    # for null.
    valid_currently: Optional[bool]
    # Required when valid_currently=false. Must quote the bounding evidence
    # from the resolved window (the words that gave the agent confidence
    # the state/event has ended). Empty string when valid_currently=null.
    validity_reason: str

    # Compact semantic label distinct from `label`. Use when the structural
    # label is generic (e.g. State.label='Residence') but the specific
    # instance has a clearer phrase ('Lives in Bellevue'). Empty when not
    # adding signal.
    semantic_label: str

    confidence: float


class AgentForm(BaseModel):
    Nodes: List[Node]
