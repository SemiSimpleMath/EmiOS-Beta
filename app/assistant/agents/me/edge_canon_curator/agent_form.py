from typing import List, Optional

from pydantic import BaseModel, Field


class PredicateVerdict(BaseModel):
    """Per-predicate decision from the curator.

    ``verdict`` is one of:
      - "variant_of"  → ``canonical_target`` must be set to the existing
                        canon's edge_type. The predicate becomes an alias
                        of that canon. Existing edges using this predicate
                        will be rewritten to the canonical form.
      - "new_canon"   → the predicate is a genuinely distinct concept worth
                        promoting. ``canonical_target`` should be the
                        predicate itself (or a normalized form). It gets
                        inserted into ``edge_canon`` as a new canonical.
      - "not_yet"     → not enough evidence to canonicalize. Leave it as
                        a raw predicate; revisit next sweep.
    """
    predicate: str = Field(..., description="The novel predicate being judged (verbatim from input).")
    verdict: str = Field(..., description="One of: variant_of | new_canon | not_yet.")
    canonical_target: Optional[str] = Field(
        None,
        description=(
            "When verdict=variant_of, the edge_type of the existing canon "
            "this predicate maps to. When verdict=new_canon, the canonical "
            "name (often the predicate itself, possibly normalized). When "
            "verdict=not_yet, leave null."
        ),
    )
    reason: str = Field(..., description="≤2 sentences explaining the verdict.")


class AgentForm(BaseModel):
    """Output of me::edge_canon_curator.

    Returns one verdict per input predicate. The predicate string MUST
    match the input verbatim — no inventing or paraphrasing.
    """
    verdicts: List[PredicateVerdict] = Field(
        default_factory=list,
        description="One verdict per input predicate, in any order.",
    )
