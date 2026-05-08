from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class EvidenceItem(BaseModel):
    query: str = Field(
        description="The exact SQL or tool call that produced this evidence.",
    )
    finding: str = Field(
        description="The result that mattered — a count, a comparison, a sample row.",
    )


class FinalAnswerDataItem(BaseModel):
    data_type: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    url: Optional[str] = None
    link: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None


class AgentForm(BaseModel):
    """Investigation report shape.

    The investigator produces a PROSE `recommendation` that describes
    EITHER (a) what KG mutations should happen and why, OR (b) why no
    mutation is needed. The `take_action` flag tells downstream which
    case this is — it's the load-bearing structural signal.

    Routing (driven by take_action × disposition):
    - take_action=False                    → finding closed as 'dismissed';
                                             recommendation prose preserved
                                             as a verdict for future agents.
                                             disposition is ignored.
    - take_action=True, auto_apply         → dev page, 24h grace timer,
                                             executor runs after window.
    - take_action=True, needs_user_review  → user queue, no timer; user must
                                             respond before executor runs.
                                             `user_question` is the specific
                                             question they need to answer.
    """

    # ---- load-bearing ----
    take_action: bool = Field(
        description=(
            "True when a KG mutation should be applied (the executor will "
            "run the recommendation). False when the verdict is 'no "
            "mutation needed' — e.g., the candidate findings are actually "
            "distinct, the data is correct as-is, or the flagged "
            "contradiction is a false positive. When False, the finding "
            "closes as 'dismissed' and the verdict_memo + verdict_node_ids "
            "are preserved for future agents."
        ),
    )
    verdict_type: Optional[str] = Field(
        default=None,
        description=(
            "REQUIRED when take_action=False. Categorical handle that "
            "downstream agents filter on. Pick one: "
            "'distinct' (two named nodes are NOT the same thing — pairwise), "
            "'verified' (data on this node is correct as-is — single-node), "
            "'false_positive' (the flagged issue isn't real), "
            "'obsolete' (finding refers to data that's been superseded since "
            "it was raised), "
            "'irrelevant' (finding type doesn't apply to this case). "
            "When take_action=True, leave null."
        ),
    )
    verdict_memo: Optional[str] = Field(
        default=None,
        description=(
            "REQUIRED when take_action=False. One short line (target ~12 "
            "words, max 25). Imperative form, names node ids inline. "
            "This is what future agents will read to skip re-investigating "
            "the same question. Examples: "
            "'do not merge 42bc6a1b with 929ee949 — concept vs household pets', "
            "'start_date 2024-09-01 on 8a3f confirmed by unified_log:5678', "
            "'state_missing_dates false positive on 4c91 — locked era'. "
            "When take_action=True, leave null."
        ),
    )
    verdict_node_ids: List[str] = Field(
        default_factory=list,
        description=(
            "REQUIRED when take_action=False (1-3 ids). The node ids this "
            "verdict pertains to. Future agents (duplicate-finder, "
            "investigator, proposal_promoter) look up these ids to find "
            "prior verdicts and skip already-decided cases. When "
            "take_action=True, leave empty."
        ),
    )
    recommendation: str = Field(
        description=(
            "Prose. When take_action=True: the plan — what KG mutations "
            "should happen and why; cite specific node ids, dates, and "
            "fields. When take_action=False: the verdict — why no "
            "mutation is needed; cite the evidence that settles it. "
            "Either way the reasoning should be readable enough that a "
            "human can audit it and decide whether they agree. Do NOT "
            "mention wiki page or entity card regen — those are auto-"
            "handled downstream."
        ),
    )
    disposition: str = Field(
        description=(
            "Routing decision when take_action=True (ignored when False). "
            "'auto_apply' (most cases — recommendation is confident enough "
            "to run after a 24h grace window) OR 'needs_user_review' "
            "(genuine ambiguity — only the user can decide; e.g., dates "
            "are missing and the source doesn't disambiguate, or two "
            "plausible interpretations conflict)."
        ),
    )
    user_question: Optional[str] = Field(
        default=None,
        description=(
            "Required when disposition='needs_user_review'. The specific "
            "question the user needs to answer. Example: 'Did Annika stop "
            "art lessons before or after the cabin trip Nov 5-12?'"
        ),
    )
    confidence: float = Field(
        description=(
            "Investigator confidence in the recommendation, 0.0-1.0. Used "
            "for filtering / sorting on the dev page; auto-apply gating "
            "happens via disposition rather than a confidence threshold."
        ),
    )

    # ---- dev-page detail / debugging ----
    diagnosis: str = Field(
        description=(
            "One-paragraph plain-language statement of what was found. "
            "Lead with the conclusion. Cite key numbers (counts, dates, "
            "ids) inline. Used for the dev-page expand-detail view."
        ),
    )
    evidence: List[EvidenceItem] = Field(
        default_factory=list,
        description=(
            "Ordered list of (query, finding) pairs that ground the "
            "diagnosis. At least one item unless the investigation truly "
            "returned nothing."
        ),
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Questions the data couldn't answer. When disposition="
            "'needs_user_review', the load-bearing question goes in "
            "`user_question`; secondary unanswered questions go here."
        ),
    )

    # ---- standard envelope (consumed by manager_exit_node + room formatters) ----
    final_answer_answer: str = Field(
        description=(
            "Markdown rendering of the structured fields above, suitable "
            "for human reading. Sections: ## Recommendation, ## Diagnosis, "
            "## Evidence, ## Open questions (omit empty sections)."
        ),
    )
    result_summary: str = Field(
        default="",
        description="One-sentence outcome for downstream agents (max ~150 chars).",
    )
    final_answer_sources: List[str] = Field(default_factory=list)
    final_answer_detail_level: str = "full"
    final_answer_data_list: List[FinalAnswerDataItem] = Field(default_factory=list)
    final_answer_task: Optional[str] = ""
    final_answer_what_was_done: Optional[str] = ""
    final_answer_interesting_info: Optional[str] = ""

    @model_validator(mode="after")
    def _enforce_conditional_contract(self):
        """The verdict_*/disposition fields are conditionally required;
        the type system can't express that, so we enforce here.

        - take_action=False  → verdict_type / verdict_memo / verdict_node_ids
                                 must all be set.
        - take_action=True + needs_user_review → user_question must be set.

        Raising at parse time means the manager_exit path never persists
        a malformed report (better than discovering the breakage at the
        finding_processor branch and silently escalating).
        """
        if self.take_action is False:
            missing = [
                name for name, val in (
                    ("verdict_type", (self.verdict_type or "").strip()),
                    ("verdict_memo", (self.verdict_memo or "").strip()),
                    ("verdict_node_ids", self.verdict_node_ids),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    f"take_action=False requires {missing}; the verdict "
                    f"is the durable record. Fill them or set take_action=True."
                )
        elif self.take_action is True:
            if (self.disposition or "").strip() == "needs_user_review" and not (self.user_question or "").strip():
                raise ValueError(
                    "disposition='needs_user_review' requires user_question — "
                    "the specific question the user must answer."
                )
        return self
