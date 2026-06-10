from typing import Literal

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Verdict on one entity card: is it worth its prompt tokens?"""

    verdict: Literal["pass", "rewrite", "veto"] = Field(
        description="pass = entity meaningful AND card content operational. "
                    "rewrite = entity meaningful but the card text is vacuous "
                    "(taxonomic/bare-valence) relative to its evidence. "
                    "veto = the entity itself does not merit a card."
    )
    reason: str = Field(
        description="One short sentence justifying the verdict. For rewrite, "
                    "name the operational content the evidence supports."
    )
