from typing import Literal

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Relation between two beliefs, for belief_engine::merge_verifier.

    Not a sameness yes/no. Two beliefs that are not duplicates can still stand in a
    relation the store must act on — most importantly they can be irreconcilable, which
    the old boolean recorded as "different" and then never revisited.
    """

    relation: Literal["same", "different", "specialises", "supersedes", "contradicts"] = Field(
        description=(
            "same — one belief written twice; they will be merged.\n"
            "different — different subject, or a differing load-bearing detail; both stand.\n"
            "specialises — one is the other plus a condition or exception; both stand, the "
            "narrower one qualifying the broader.\n"
            "supersedes — SAME subject, OPPOSING claims, and the evidence shows one is a later "
            "state of the other (something changed); the older one will be deprecated.\n"
            "contradicts — SAME subject, OPPOSING claims, and the evidence does NOT establish "
            "which is current; both will be marked contested for full re-evaluation."
        )
    )
    reason: str = Field(
        description=(
            "Short justification. Name what each belief targets and, for supersedes vs "
            "contradicts, the dated evidence that did or did not settle which is current."
        )
    )
    canonical_statement: str = Field(
        default="",
        description=(
            "ONLY when relation='same': the single reconciled statement replacing both. Carry "
            "over every load-bearing detail from each — if one is the fuller version, return "
            "the fuller one; if each adds something the other lacks, combine them faithfully. "
            "Never drop a clause, qualifier or instruction, and never add a claim neither makes. "
            "Empty for every other relation."
        ),
    )
    current_side: Literal["", "a", "b"] = Field(
        default="",
        description=(
            "ONLY when relation='supersedes': which side is the CURRENT state — 'a' or 'b'. The "
            "other is the outdated one and will be deprecated. Empty for every other relation."
        ),
    )
