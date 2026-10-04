"""Output schema for subconscious::story_concern: one concern's story and where it is going, for the owner."""
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(description="Where it stands now, in three to six words, as a label.")
    story: str = Field(description="What has happened with it so far, in two to four sentences.")
    proposed: List[str] = Field(description="The things proposed now, each as one plain sentence. Empty when "
                                            "nothing is proposed.")
    fate: str = Field(description="Where it is heading: what happens next and when, and what would settle it, "
                                  "in one or two sentences.")
